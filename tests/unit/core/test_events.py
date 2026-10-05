"""Unit tests for core events and realtime event constructors.

Tests the DomainEvent contract, frozen immutability, JSON roundtrip serialization,
and all make_*_change event constructors in core.realtime.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4
import pytest
from pydantic import ValidationError

from core.events import DomainEvent
from core.realtime import (
    DashboardChanged,
    IngestionChanged,
    KnowledgeChanged,
    SourceChanged,
    make_dashboard_change,
    make_graph_change,
    make_index_change,
    make_ingestion_change,
    make_knowledge_change,
    make_source_change,
    make_timeline_change,
    make_timeline_collection_change,
)


class TestDomainEvent:
    """Test suite for the core DomainEvent base contract."""

    def test_domain_event_instantiation_and_attributes(self) -> None:
        """DomainEvent initializes correctly with all required attributes."""
        event_id = uuid4()
        now = datetime.now(UTC)
        payload = {"entity_key": "user_123", "action": "created", "count": 1}

        event = DomainEvent(
            id=event_id,
            type="user.created",
            version=1,
            occurred_at=now,
            producer="test_service",
            payload=payload,
        )

        assert event.id == event_id
        assert event.type == "user.created"
        assert event.version == 1
        assert event.occurred_at == now
        assert event.producer == "test_service"
        assert event.payload == payload

    def test_domain_event_frozen_immutability(self) -> None:
        """DomainEvent is frozen; any attribute modification raises ValidationError."""
        event = DomainEvent(
            id=uuid4(),
            type="immutable.test",
            version=1,
            occurred_at=datetime.now(UTC),
            producer="tester",
            payload={},
        )

        with pytest.raises(ValidationError):
            event.version = 2  # type: ignore[misc]

        with pytest.raises(ValidationError):
            event.payload = {"new": "value"}  # type: ignore[misc]

    def test_domain_event_forbids_extra_fields(self) -> None:
        """DomainEvent rejects undeclared extra attributes."""
        with pytest.raises(ValidationError):
            DomainEvent(
                id=uuid4(),
                type="test.event",
                version=1,
                occurred_at=datetime.now(UTC),
                producer="tester",
                payload={},
                extra_field="disallowed",  # type: ignore[call-arg]
            )

    def test_domain_event_json_serialization_roundtrip(self) -> None:
        """DomainEvent supports exact serialization and deserialization via JSON."""
        event_id = uuid4()
        now = datetime.now(UTC)
        original = DomainEvent(
            id=event_id,
            type="source.ingested",
            version=3,
            occurred_at=now,
            producer="ingestion_pipeline",
            payload={"status": "completed", "nested": {"count": 42}},
        )

        serialized = original.model_dump_json()
        assert isinstance(serialized, str)

        restored = DomainEvent.model_validate_json(serialized)
        assert restored.id == original.id
        assert restored.type == original.type
        assert restored.version == original.version
        assert restored.occurred_at == original.occurred_at
        assert restored.producer == original.producer
        assert restored.payload == original.payload
        assert restored == original

    def test_domain_event_utc_timestamps(self) -> None:
        """DomainEvent preserves timezone awareness and UTC timestamp comparison."""
        now_utc = datetime.now(UTC)
        offset_tz = timezone(timedelta(hours=7))
        now_vietnam = datetime.now(offset_tz)

        event_utc = DomainEvent(
            id=uuid4(),
            type="tz.test",
            version=1,
            occurred_at=now_utc,
            producer="test",
            payload={},
        )
        event_vn = DomainEvent(
            id=uuid4(),
            type="tz.test",
            version=1,
            occurred_at=now_vietnam,
            producer="test",
            payload={},
        )

        assert event_utc.occurred_at.tzinfo is not None
        assert event_vn.occurred_at.tzinfo is not None

    def test_domain_event_missing_fields_validation_error(self) -> None:
        """DomainEvent raises ValidationError if any required field is missing."""
        with pytest.raises(ValidationError):
            DomainEvent(id=uuid4(), type="test")  # type: ignore[call-arg]

    def test_domain_event_invalid_type_validation_error(self) -> None:
        """DomainEvent raises ValidationError when given incompatible field types."""
        with pytest.raises(ValidationError):
            DomainEvent(
                id="not-a-uuid",  # type: ignore[arg-type]
                type="test",
                version="not-an-int",  # type: ignore[arg-type]
                occurred_at="not-a-datetime",  # type: ignore[arg-type]
                producer="test",
                payload="not-a-dict",  # type: ignore[arg-type]
            )


class TestRealtimeEventConstructors:
    """Test suite for the domain change constructors in core.realtime."""

    def test_make_source_change_valid(self) -> None:
        """make_source_change builds a valid SourceChanged event."""
        src_id = uuid4()
        op_id = uuid4()
        evt = make_source_change(
            source_id=src_id,
            generation=2,
            status="active",
            connector_state="synced",
            operation_id=op_id,
        )

        assert isinstance(evt, SourceChanged)
        assert evt.type == "source.changed"
        assert evt.source_id == src_id
        assert evt.generation == 2
        assert evt.status == "active"
        assert evt.connector_state == "synced"
        assert evt.operation_id == op_id
        assert evt.schema_version == 1

    def test_make_source_change_invalid_generation(self) -> None:
        """make_source_change rejects generation < 1."""
        with pytest.raises(ValidationError):
            make_source_change(
                source_id=uuid4(),
                generation=0,
                status="active",
            )

    def test_make_source_change_invalid_status(self) -> None:
        """make_source_change rejects unsupported status strings."""
        with pytest.raises(ValidationError):
            make_source_change(
                source_id=uuid4(),
                generation=1,
                status="invalid_status",  # type: ignore[arg-type]
            )

    def test_make_ingestion_change_valid(self) -> None:
        """make_ingestion_change creates a validated IngestionChanged event."""
        src_id = uuid4()
        run_id = uuid4()
        evt = make_ingestion_change(
            source_id=src_id,
            run_id=run_id,
            status="running",
            stage_key="parsing",
            stage_status="pending",
        )

        assert isinstance(evt, IngestionChanged)
        assert evt.type == "ingestion.changed"
        assert evt.source_id == src_id
        assert evt.run_id == run_id
        assert evt.status == "running"
        assert evt.stage_key == "parsing"
        assert evt.stage_status == "pending"

    def test_make_ingestion_change_invalid_status(self) -> None:
        """make_ingestion_change rejects invalid run status."""
        with pytest.raises(ValidationError):
            make_ingestion_change(
                source_id=uuid4(),
                run_id=uuid4(),
                status="not_a_valid_status",  # type: ignore[arg-type]
            )

    def test_make_knowledge_change_valid(self) -> None:
        """make_knowledge_change creates source-scope KnowledgeChanged events."""
        src_id = uuid4()
        doc_id = uuid4()
        evt = make_knowledge_change(
            source_id=src_id,
            document_id=doc_id,
            version=1,
            deleted=False,
        )

        assert isinstance(evt, KnowledgeChanged)
        assert evt.type == "knowledge.changed"
        assert evt.scope == "source"
        assert evt.source_id == src_id
        assert evt.document_id == doc_id
        assert evt.version == 1
        assert not evt.deleted

    def test_make_index_change_valid(self) -> None:
        """make_index_change creates index-scope KnowledgeChanged events."""
        gen_id = uuid4()
        evt = make_index_change(
            generation_id=gen_id,
            status="active",
            indexed_items=100,
            failed_items=2,
        )

        assert evt.scope == "index"
        assert evt.index_generation_id == gen_id
        assert evt.index_status == "active"
        assert evt.indexed_items == 100
        assert evt.failed_items == 2

    def test_make_graph_change_valid(self) -> None:
        """make_graph_change creates graph events with mutually exclusive entity/relationship."""
        ent_id = uuid4()
        evt1 = make_graph_change(entity_id=ent_id)
        assert evt1.scope == "graph"
        assert evt1.entity_id == ent_id
        assert evt1.relationship_id is None

        rel_id = uuid4()
        evt2 = make_graph_change(relationship_id=rel_id, deleted=True)
        assert evt2.scope == "graph"
        assert evt2.relationship_id == rel_id
        assert evt2.entity_id is None
        assert evt2.deleted is True

    def test_make_graph_change_mutual_exclusion_violation(self) -> None:
        """make_graph_change rejects when both entity and relationship are supplied or both None."""
        with pytest.raises(ValidationError):
            make_graph_change(entity_id=uuid4(), relationship_id=uuid4())

        with pytest.raises(ValidationError):
            make_graph_change(entity_id=None, relationship_id=None)

    def test_make_timeline_change_valid(self) -> None:
        """make_timeline_change creates timeline events with event identity and revision."""
        evt_id = uuid4()
        evt = make_timeline_change(event_id=evt_id, revision=3, deleted=True)
        assert evt.scope == "timeline"
        assert evt.event_id == evt_id
        assert evt.event_revision == 3
        assert evt.deleted is True

    def test_make_timeline_collection_change_valid(self) -> None:
        """make_timeline_collection_change accepts exactly one source or entity."""
        src_id = uuid4()
        evt1 = make_timeline_collection_change(source_id=src_id)
        assert evt1.scope == "timeline_collection"
        assert evt1.source_id == src_id
        assert evt1.entity_id is None

        ent_id = uuid4()
        evt2 = make_timeline_collection_change(entity_id=ent_id)
        assert evt2.scope == "timeline_collection"
        assert evt2.entity_id == ent_id
        assert evt2.source_id is None

    def test_make_timeline_collection_change_invalid_exclusion(self) -> None:
        """make_timeline_collection_change raises ValueError if neither or both are supplied."""
        with pytest.raises(ValueError, match="exactly one"):
            make_timeline_collection_change(source_id=None, entity_id=None)

        with pytest.raises(ValueError, match="exactly one"):
            make_timeline_collection_change(source_id=uuid4(), entity_id=uuid4())

    def test_make_dashboard_change_valid(self) -> None:
        """make_dashboard_change creates validated DashboardChanged events."""
        res_id = uuid4()
        evt = make_dashboard_change(scope="dashboard", resource_id=res_id, revision=5, deleted=False)
        assert isinstance(evt, DashboardChanged)
        assert evt.type == "dashboard.changed"
        assert evt.scope == "dashboard"
        assert evt.id == res_id
        assert evt.revision == 5
        assert evt.deleted is False

    def test_make_dashboard_change_invalid_scope_or_revision(self) -> None:
        """make_dashboard_change rejects invalid scopes or non-positive revisions."""
        with pytest.raises(ValidationError):
            make_dashboard_change(scope="invalid", resource_id=uuid4(), revision=1)  # type: ignore[arg-type]

        with pytest.raises(ValidationError):
            make_dashboard_change(scope="dashboard", resource_id=uuid4(), revision=0)
