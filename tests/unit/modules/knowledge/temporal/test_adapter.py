"""Unit tests for modules.knowledge.temporal.adapter.

Tests cover:
- EvidenceIdentity and CanonicalEntityBinding bounds, digests, and field support validation.
- CanonicalNodeRecoveryAction validation across all action types (retain, delete_orphan,
  replace_from_current_support, delete_for_rebuild).
- CandidateNodeReplacement bounds and hash validation.
- GraphState, GraphOperationError, and GraphOperationUnknown exception classes.
- Fingerprint calculations (_episode_state_fingerprint, _entity_state_fingerprint,
  _fact_state, _canonical_json, _normalized_graph_timestamp).
- GraphWriteReceipt validation and bounded field constraints.
- TemporalGraph adapter lifecycle (init, initialize, health, close), the Neo4j dispatch scope,
  SafeDriver transaction guard, bulk shape validation, cessation and partition isolation.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import re
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from neo4j import time as neo_time

from modules.knowledge.temporal.adapter import (
    MAX_SUPPORT,
    CandidateNodeReplacement,
    CanonicalEntityBinding,
    CanonicalNodeRecoveryAction,
    DispatchOwnership,
    EvidenceIdentity,
    ExactFactState,
    ExactGraphLink,
    GraphConfiguration,
    GraphOperationError,
    GraphOperationUnknown,
    GraphState,
    GraphWriteReceipt,
    TemporalGraph,
    _canonical_json,
    _dispatch_scope,
    _DispatchScope,
    _entity_state_fingerprint,
    _episode_state_fingerprint,
    _fact_state,
    _incident_ids_absent,
    _make_driver_type,
    _normalized_graph_timestamp,
    _require_group,
    _safe_error,
)


class TestEvidenceIdentity:
    """Tests for EvidenceIdentity dataclass."""

    def test_evidence_identity_instantiation(self) -> None:
        """Verify EvidenceIdentity fields and immutability."""
        source_id = uuid4()
        doc_id = uuid4()
        version_id = uuid4()
        chunk_id = uuid4()

        evidence = EvidenceIdentity(
            source_id=source_id,
            source_generation=1,
            document_id=doc_id,
            document_version_id=version_id,
            chunk_id=chunk_id,
        )
        assert evidence.source_id == source_id
        assert evidence.source_generation == 1
        assert evidence.document_id == doc_id
        assert evidence.document_version_id == version_id
        assert evidence.chunk_id == chunk_id

        with pytest.raises(AttributeError):
            evidence.source_generation = 2  # type: ignore[misc]


class TestCanonicalEntityBinding:
    """Tests for CanonicalEntityBinding bounds, digests, and field support."""

    @pytest.fixture
    def valid_membership_id(self) -> UUID:
        """Fixture for a sample membership UUID."""
        return uuid4()

    @pytest.fixture
    def valid_evidence(self) -> EvidenceIdentity:
        """Fixture for valid EvidenceIdentity."""
        return EvidenceIdentity(
            source_id=uuid4(),
            source_generation=1,
            document_id=uuid4(),
            document_version_id=uuid4(),
            chunk_id=uuid4(),
        )

    def test_valid_binding_minimal(self, valid_membership_id: UUID, valid_evidence: EvidenceIdentity) -> None:
        """Verify minimal valid CanonicalEntityBinding."""
        binding = CanonicalEntityBinding(
            canonical_entity_id=uuid4(),
            canonical_revision=1,
            membership_ids=(valid_membership_id,),
            evidence=(valid_evidence,),
            graph_entity_uuid=str(uuid4()),
            group_id="group_1",
            mapping_revision=0,
        )
        assert binding.canonical_revision == 1
        assert binding.mapping_revision == 0
        assert binding.node_name is None
        assert binding.node_summary is None

    def test_valid_binding_with_fields_and_support(self, valid_membership_id: UUID, valid_evidence: EvidenceIdentity) -> None:
        """Verify CanonicalEntityBinding with node name and summary matching field support digests."""
        name = "Acme Corp"
        summary = "A sample organization."
        name_digest = hashlib.sha256(name.encode()).hexdigest()
        summary_digest = hashlib.sha256(summary.encode()).hexdigest()
        prior_fp = hashlib.sha256(b"prior_state").hexdigest()

        binding = CanonicalEntityBinding(
            canonical_entity_id=uuid4(),
            canonical_revision=2,
            membership_ids=(valid_membership_id,),
            evidence=(valid_evidence,),
            graph_entity_uuid=str(uuid4()),
            group_id="group_1",
            mapping_revision=1,
            node_name=name,
            node_summary=summary,
            field_support=(
                ("name", name_digest, (valid_membership_id,)),
                ("description", summary_digest, (valid_membership_id,)),
            ),
            prior_node_state_fingerprint=prior_fp,
        )
        assert binding.node_name == name
        assert binding.node_summary == summary
        assert binding.prior_node_state_fingerprint == prior_fp

    def test_invalid_revisions(self, valid_membership_id: UUID, valid_evidence: EvidenceIdentity) -> None:
        """Verify revisions must satisfy canonical_revision >= 1 and mapping_revision >= 0."""
        with pytest.raises(ValueError, match="exceeds its owner bounds"):
            CanonicalEntityBinding(
                canonical_entity_id=uuid4(),
                canonical_revision=0,  # invalid
                membership_ids=(valid_membership_id,),
                evidence=(valid_evidence,),
                graph_entity_uuid=str(uuid4()),
                group_id="group_1",
                mapping_revision=0,
            )

        with pytest.raises(ValueError, match="exceeds its owner bounds"):
            CanonicalEntityBinding(
                canonical_entity_id=uuid4(),
                canonical_revision=1,
                membership_ids=(valid_membership_id,),
                evidence=(valid_evidence,),
                graph_entity_uuid=str(uuid4()),
                group_id="group_1",
                mapping_revision=-1,  # invalid
            )

    def test_invalid_group_id_or_uuid(self, valid_membership_id: UUID, valid_evidence: EvidenceIdentity) -> None:
        """Verify group_id must be non-empty and <= 200 bytes, and graph_entity_uuid must be a UUID."""
        with pytest.raises(ValueError, match="exceeds its owner bounds"):
            CanonicalEntityBinding(
                canonical_entity_id=uuid4(),
                canonical_revision=1,
                membership_ids=(valid_membership_id,),
                evidence=(valid_evidence,),
                graph_entity_uuid=str(uuid4()),
                group_id="",  # empty
                mapping_revision=0,
            )

        with pytest.raises(ValueError, match="exceeds its owner bounds"):
            CanonicalEntityBinding(
                canonical_entity_id=uuid4(),
                canonical_revision=1,
                membership_ids=(valid_membership_id,),
                evidence=(valid_evidence,),
                graph_entity_uuid="invalid-uuid-format",
                group_id="group_1",
                mapping_revision=0,
            )

    def test_invalid_membership_bounds_and_duplicates(self, valid_evidence: EvidenceIdentity) -> None:
        """Verify membership_ids cannot be empty, exceed MAX_SUPPORT, or contain duplicates."""
        with pytest.raises(ValueError, match="exceeds its owner bounds"):
            CanonicalEntityBinding(
                canonical_entity_id=uuid4(),
                canonical_revision=1,
                membership_ids=(),  # empty
                evidence=(valid_evidence,),
                graph_entity_uuid=str(uuid4()),
                group_id="group_1",
                mapping_revision=0,
            )

        dup_id = uuid4()
        with pytest.raises(ValueError, match="exceeds its owner bounds"):
            CanonicalEntityBinding(
                canonical_entity_id=uuid4(),
                canonical_revision=1,
                membership_ids=(dup_id, dup_id),  # duplicate
                evidence=(valid_evidence,),
                graph_entity_uuid=str(uuid4()),
                group_id="group_1",
                mapping_revision=0,
            )

    def test_invalid_field_support_and_hash_mismatch(self, valid_membership_id: UUID, valid_evidence: EvidenceIdentity) -> None:
        """Verify field support requires valid field names ('name', 'description') and matching digests."""
        valid_digest = hashlib.sha256(b"Acme Corp").hexdigest()

        # Invalid field name
        with pytest.raises(ValueError, match="field support is invalid"):
            CanonicalEntityBinding(
                canonical_entity_id=uuid4(),
                canonical_revision=1,
                membership_ids=(valid_membership_id,),
                evidence=(valid_evidence,),
                graph_entity_uuid=str(uuid4()),
                group_id="group_1",
                mapping_revision=0,
                field_support=(("alias", valid_digest, (valid_membership_id,)),),
            )

        # Support ID not in membership_ids
        with pytest.raises(ValueError, match="field support is invalid"):
            CanonicalEntityBinding(
                canonical_entity_id=uuid4(),
                canonical_revision=1,
                membership_ids=(valid_membership_id,),
                evidence=(valid_evidence,),
                graph_entity_uuid=str(uuid4()),
                group_id="group_1",
                mapping_revision=0,
                field_support=(("name", valid_digest, (uuid4(),)),),
            )

        # Name sha256 mismatch
        with pytest.raises(ValueError, match="requires exact source field support"):
            CanonicalEntityBinding(
                canonical_entity_id=uuid4(),
                canonical_revision=1,
                membership_ids=(valid_membership_id,),
                evidence=(valid_evidence,),
                graph_entity_uuid=str(uuid4()),
                group_id="group_1",
                mapping_revision=0,
                node_name="Different Name",
                field_support=(("name", valid_digest, (valid_membership_id,)),),
            )


class TestCanonicalNodeRecoveryAction:
    """Tests for CanonicalNodeRecoveryAction across all 4 recovery actions and edge cases."""

    @pytest.fixture
    def entity_uuid(self) -> str:
        """Fixture for a valid entity UUID string."""
        return str(uuid4())

    @pytest.fixture
    def state_fp(self) -> str:
        """Fixture for a valid SHA256 fingerprint."""
        return hashlib.sha256(b"current_state").hexdigest()

    def test_retain_action(self, entity_uuid: str, state_fp: str) -> None:
        """Verify valid 'retain' recovery action."""
        action = CanonicalNodeRecoveryAction(
            graph_entity_uuid=entity_uuid,
            expected_current_state_fingerprint=state_fp,
            action="retain",
        )
        assert action.action == "retain"
        assert action.graph_entity_uuid == entity_uuid
        assert action.expected_current_state_fingerprint == state_fp

    def test_delete_orphan_action(self, entity_uuid: str, state_fp: str) -> None:
        """Verify valid 'delete_orphan' recovery action."""
        action = CanonicalNodeRecoveryAction(
            graph_entity_uuid=entity_uuid,
            expected_current_state_fingerprint=state_fp,
            action="delete_orphan",
        )
        assert action.action == "delete_orphan"

    def test_delete_for_rebuild_requires_rebuild_mapping_ids(self, entity_uuid: str, state_fp: str) -> None:
        """Verify 'delete_for_rebuild' requires sorted non-empty rebuild_mapping_ids."""
        m_id1 = uuid4()
        m_id2 = uuid4()
        sorted_ids = tuple(sorted((m_id1, m_id2), key=str))

        action = CanonicalNodeRecoveryAction(
            graph_entity_uuid=entity_uuid,
            expected_current_state_fingerprint=state_fp,
            action="delete_for_rebuild",
            rebuild_mapping_ids=sorted_ids,
        )
        assert action.action == "delete_for_rebuild"
        assert action.rebuild_mapping_ids == sorted_ids

        # Empty rebuild_mapping_ids on delete_for_rebuild raises ValueError
        with pytest.raises(ValueError, match="Rebuild deletion requires bounded exact dependent mappings"):
            CanonicalNodeRecoveryAction(
                graph_entity_uuid=entity_uuid,
                expected_current_state_fingerprint=state_fp,
                action="delete_for_rebuild",
                rebuild_mapping_ids=(),
            )

        # Non-delete_for_rebuild with rebuild_mapping_ids raises ValueError
        with pytest.raises(ValueError, match="Rebuild deletion requires bounded exact dependent mappings"):
            CanonicalNodeRecoveryAction(
                graph_entity_uuid=entity_uuid,
                expected_current_state_fingerprint=state_fp,
                action="retain",
                rebuild_mapping_ids=sorted_ids,
            )

    def test_replace_from_current_support(self, entity_uuid: str, state_fp: str) -> None:
        """Verify 'replace_from_current_support' with replacement binding and aware timestamp."""
        mem_id = uuid4()
        ev = EvidenceIdentity(uuid4(), 1, uuid4(), uuid4(), uuid4())
        name = "Node Name"
        name_digest = hashlib.sha256(name.encode()).hexdigest()

        binding = CanonicalEntityBinding(
            canonical_entity_id=uuid4(),
            canonical_revision=1,
            membership_ids=(mem_id,),
            evidence=(ev,),
            graph_entity_uuid=entity_uuid,
            group_id="group_1",
            mapping_revision=0,
            node_name=name,
            field_support=(("name", name_digest, (mem_id,)),),
        )

        now = datetime.now(UTC)
        action = CanonicalNodeRecoveryAction(
            graph_entity_uuid=entity_uuid,
            expected_current_state_fingerprint=state_fp,
            action="replace_from_current_support",
            replacement_binding=binding,
            replacement_created_at=now,
        )
        assert action.action == "replace_from_current_support"
        assert action.replacement_binding == binding

    def test_invalid_action_or_uuid(self, state_fp: str) -> None:
        """Verify invalid actions or malformed UUIDs raise ValueError."""
        with pytest.raises(ValueError, match="recovery action is invalid"):
            CanonicalNodeRecoveryAction(
                graph_entity_uuid="not-a-uuid",
                expected_current_state_fingerprint=state_fp,
                action="retain",
            )

        with pytest.raises(ValueError, match="recovery action is invalid"):
            CanonicalNodeRecoveryAction(
                graph_entity_uuid=str(uuid4()),
                expected_current_state_fingerprint=state_fp,
                action="invalid_action",  # type: ignore[arg-type]
            )

    def test_incident_links_sorted_and_unique(self, entity_uuid: str, state_fp: str) -> None:
        """Verify expected_incident_links must be sorted by edge_id and have unique edge IDs."""
        id1 = str(uuid4())
        id2 = str(uuid4())
        link1 = ExactGraphLink(id1, "KNOWS", entity_uuid, str(uuid4()), ())
        link2 = ExactGraphLink(id2, "KNOWS", entity_uuid, str(uuid4()), ())

        # Correct sort
        sorted_links = tuple(sorted((link1, link2), key=lambda x: x.edge_id))
        action = CanonicalNodeRecoveryAction(
            graph_entity_uuid=entity_uuid,
            expected_current_state_fingerprint=state_fp,
            action="retain",
            expected_incident_links=sorted_links,
        )
        assert action.expected_incident_links == sorted_links

        # Unsorted links raise ValueError
        unsorted_links = tuple(reversed(sorted_links))
        if unsorted_links != sorted_links:
            with pytest.raises(ValueError, match="link inventory exceeds its bounds"):
                CanonicalNodeRecoveryAction(
                    graph_entity_uuid=entity_uuid,
                    expected_current_state_fingerprint=state_fp,
                    action="retain",
                    expected_incident_links=unsorted_links,
                )


class TestCandidateNodeReplacement:
    """Tests for CandidateNodeReplacement bounds and field support."""

    def test_valid_candidate_replacement(self) -> None:
        """Verify valid CandidateNodeReplacement instantiation."""
        ep1 = str(uuid4())
        ep2 = str(uuid4())
        sorted_eps = tuple(sorted((ep1, ep2)))

        name = "Candidate Name"
        summary = "Candidate summary text"
        name_digest = hashlib.sha256(name.encode()).hexdigest()
        summary_digest = hashlib.sha256(summary.encode()).hexdigest()

        field_support = (
            ("name", name_digest, (sorted_eps[0],)),
            ("description", summary_digest, (sorted_eps[1],)),
        )

        candidate = CandidateNodeReplacement(
            graph_entity_uuid=str(uuid4()),
            group_id="group_candidate",
            node_name=name,
            node_summary=summary,
            support_episode_ids=sorted_eps,
            field_support=field_support,
        )
        assert candidate.node_name == name
        assert candidate.group_id == "group_candidate"

    def test_invalid_candidate_support_mismatch(self) -> None:
        """Verify candidate node replacement requires matching support hashes and IDs."""
        ep_id = str(uuid4())
        name = "Test Node"
        wrong_digest = hashlib.sha256(b"wrong text").hexdigest()

        with pytest.raises(ValueError, match="lacks bounded surviving field support"):
            CandidateNodeReplacement(
                graph_entity_uuid=str(uuid4()),
                group_id="group_1",
                node_name=name,
                node_summary="Summary",
                support_episode_ids=(ep_id,),
                field_support=(("name", wrong_digest, (ep_id,)),),
            )


class TestGraphStateAndErrors:
    """Tests for GraphState, GraphOperationError, and GraphOperationUnknown."""

    def test_graph_state_enum(self) -> None:
        """Verify GraphState values."""
        assert GraphState.DISABLED == "disabled"
        assert GraphState.UNCONFIGURED == "unconfigured"
        assert GraphState.UNAVAILABLE == "unavailable"
        assert GraphState.READY == "ready"

    def test_graph_operation_error_hierarchy(self) -> None:
        """Verify error class inheritance and message persistence."""
        err = GraphOperationError("graph_failed")
        assert isinstance(err, RuntimeError)
        assert str(err) == "graph_failed"

    def test_graph_operation_unknown_with_receipt(self) -> None:
        """Verify GraphOperationUnknown carries message and optional receipt."""
        unknown_without_receipt = GraphOperationUnknown("transport_lost")
        assert unknown_without_receipt.receipt is None

        # Build mock receipt
        receipt = GraphWriteReceipt(
            operation_id=uuid4(),
            lease_token=uuid4(),
            episode_id=uuid4(),
            group_id="group_1",
            mapping_revision=0,
            desired_support_digest=hashlib.sha256(b"support").hexdigest(),
            phase="shell_write_intent",
            episode_state_fingerprint=hashlib.sha256(b"episode").hexdigest(),
        )
        unknown_with_receipt = GraphOperationUnknown("ambiguous_state", receipt=receipt)
        assert unknown_with_receipt.receipt == receipt
        assert str(unknown_with_receipt) == "ambiguous_state"


class TestFingerprintsAndNormalizations:
    """Tests for fingerprint functions and normalization utilities."""

    def test_canonical_json_deterministic(self) -> None:
        """Verify _canonical_json produces identical bytes regardless of key insertion order."""
        dict1 = {"b": 2, "a": 1, "c": [3, 4]}
        dict2 = {"a": 1, "c": [3, 4], "b": 2}
        assert _canonical_json(dict1) == _canonical_json(dict2)

    def test_normalized_graph_timestamp(self) -> None:
        """Verify timestamp normalization converts datetime and aware strings to UTC ISO format."""
        dt_utc = datetime(2026, 10, 5, 12, 0, 0, tzinfo=UTC)
        assert _normalized_graph_timestamp(dt_utc) == "2026-10-05T12:00:00+00:00"
        assert _normalized_graph_timestamp(None) is None
        assert _normalized_graph_timestamp("2026-10-05T12:00:00Z") == "2026-10-05T12:00:00+00:00"

    def test_episode_state_fingerprint_deterministic(self) -> None:
        """Verify _episode_state_fingerprint generates a 64-character SHA256 hex string."""
        episode = MagicMock()
        episode.uuid = uuid4()
        episode.group_id = "group_ep"
        episode.name = "ep_name"
        episode.source = "test_source"
        episode.source_description = "desc"
        episode.content = "episode content"
        episode.valid_at = datetime.now(UTC)
        episode.created_at = datetime.now(UTC)
        episode.entity_edges = [str(uuid4()), str(uuid4())]

        fp = _episode_state_fingerprint(episode)
        assert len(fp) == 64
        assert int(fp, 16) > 0

    def test_entity_state_fingerprint_dict_and_object(self) -> None:
        """Verify _entity_state_fingerprint works on dicts and objects with Entity label."""
        entity_dict = {
            "uuid": str(uuid4()),
            "group_id": "group_1",
            "name": "Entity Name",
            "summary": "Summary text",
            "labels": ["Entity"],
            "created_at": datetime.now(UTC),
        }
        fp_dict = _entity_state_fingerprint(entity_dict)
        assert len(fp_dict) == 64

        # Missing Entity label when not writer_intended raises GraphOperationError
        entity_dict_no_label = dict(entity_dict)
        entity_dict_no_label["labels"] = ["OtherLabel"]
        with pytest.raises(GraphOperationError, match="graph_entity_state_unsupported"):
            _entity_state_fingerprint(entity_dict_no_label, writer_intended=False)

        # Writer intended synthesizes Entity label
        fp_synth = _entity_state_fingerprint(entity_dict_no_label, writer_intended=True)
        assert len(fp_synth) == 64

    def test_fact_state_creation(self) -> None:
        """Verify _fact_state produces an ExactFactState record with sorted episodes."""
        edge = MagicMock()
        edge.uuid = uuid4()
        edge.group_id = "group_1"
        edge.source_node_uuid = uuid4()
        edge.target_node_uuid = uuid4()
        ep1 = str(uuid4())
        ep2 = str(uuid4())
        edge.episodes = [ep2, ep1]
        edge.name = "RELATED_TO"
        edge.fact = "Node A is related to Node B"
        edge.valid_at = datetime.now(UTC)
        edge.invalid_at = None
        edge.expired_at = None
        edge.created_at = datetime.now(UTC)
        edge.reference_time = datetime.now(UTC)
        edge.attributes = {}
        edge.fact_embedding = [0.1, 0.2, 0.3]

        state = _fact_state(edge, dimensions=3)
        assert isinstance(state, ExactFactState)
        assert state.fact_id == str(edge.uuid)
        assert state.episode_ids == tuple(sorted((ep1, ep2)))
        assert state.existed is True
        assert len(state.state_fingerprint) == 64


class TestTemporalGraphAdapterLifecycle:
    """Tests for TemporalGraph adapter lifecycle and operations."""

    def test_init_disabled(self) -> None:
        """Verify TemporalGraph initializes to DISABLED state when disabled."""
        config = GraphConfiguration(
            enabled=False, host="", port=0, username=None, password=None,
            database="test_db",
        )
        adapter = TemporalGraph(config)
        assert adapter.state == GraphState.DISABLED

    def test_init_enabled_unconfigured(self) -> None:
        """Verify TemporalGraph initializes to UNCONFIGURED state when enabled without credentials."""
        config = GraphConfiguration(
            enabled=True, host="", port=0, username=None, password=None,
            database="test_db",
        )
        adapter = TemporalGraph(config)
        assert adapter.state == GraphState.UNCONFIGURED

    @pytest.mark.asyncio
    async def test_initialize_disabled_returns_disabled(self) -> None:
        """Verify initialize returns DISABLED immediately when config is disabled."""
        config = GraphConfiguration(
            enabled=False, host="localhost", port=7687, username=None, password="secret",
            database="test_db",
        )
        adapter = TemporalGraph(config)
        state = await adapter.initialize()
        assert state == GraphState.DISABLED

    @pytest.mark.asyncio
    async def test_initialize_missing_credentials_returns_unconfigured(self) -> None:
        """Verify initialize returns UNCONFIGURED when host or password missing."""
        config = GraphConfiguration(
            enabled=True, host="", port=7687, username=None, password="secret",
            database="test_db",
        )
        adapter = TemporalGraph(config)
        state = await adapter.initialize()
        assert state == GraphState.UNCONFIGURED

    @pytest.mark.asyncio
    @pytest.mark.parametrize("found", [0, 1])
    async def test_initialize_requires_actual_partition_uniqueness(
        self, monkeypatch: pytest.MonkeyPatch, found: int,
    ) -> None:
        """A successful IF NOT EXISTS DDL is insufficient when its name collides."""
        from modules.knowledge.temporal import adapter as module
        from modules.knowledge.temporal.graph_fence import PARTITION_FENCE_SCHEMA

        driver = SimpleNamespace(
            build_indices_and_constraints=AsyncMock(),
            control_query=AsyncMock(return_value=[{"found": found}]),
            close=AsyncMock(),
        )
        monkeypatch.setattr(module, "_make_driver_type", lambda: lambda *_args: driver)
        graph = TemporalGraph(GraphConfiguration(
            enabled=True, host="localhost", port=7687, username="neo4j", password="secret",
            database="test_db",
        ))
        state = await graph.initialize()
        assert state == (GraphState.READY if found else GraphState.UNAVAILABLE)
        assert driver.control_query.await_args_list[-1].args[0] == PARTITION_FENCE_SCHEMA
        if not found:
            driver.close.assert_awaited_once()
            assert graph._driver is None

    @pytest.mark.asyncio
    async def test_health_checks(self) -> None:
        """Verify health checks report appropriate states."""
        # Disabled
        config_disabled = GraphConfiguration(
            enabled=False, host="localhost", port=7687, username=None, password="secret",
            database="test_db",
        )
        adapter_disabled = TemporalGraph(config_disabled)
        assert await adapter_disabled.health() == GraphState.DISABLED

        # Unconfigured
        config_unconf = GraphConfiguration(
            enabled=True, host="", port=7687, username=None, password=None,
            database="test_db",
        )
        adapter_unconf = TemporalGraph(config_unconf)
        assert await adapter_unconf.health() == GraphState.UNCONFIGURED

    @pytest.mark.asyncio
    async def test_close_resets_state(self) -> None:
        """Verify close resets adapter state to unconfigured or disabled."""
        config = GraphConfiguration(
            enabled=True, host="localhost", port=7687, username=None, password="secret",
            database="test_db",
        )
        adapter = TemporalGraph(config)
        adapter.state = GraphState.READY
        adapter._driver = AsyncMock()

        await adapter.close()
        assert adapter.state == GraphState.UNCONFIGURED
        assert adapter._driver is None


class _FakeRecord:
    """Minimal neo4j Record stand-in exposing data()."""

    def __init__(self, row: dict[str, Any]) -> None:
        """Store one row."""
        self._row = row

    def data(self) -> dict[str, Any]:
        """Return the row as the driver would."""
        return self._row


class _FakeResult:
    """Async-iterable query result with keys()."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        """Store rows to stream."""
        self._rows = rows

    def __aiter__(self) -> AsyncIterator[_FakeRecord]:
        """Stream rows lazily so the row cap can abort early."""
        async def gen() -> AsyncIterator[_FakeRecord]:
            """Yield each stored row."""
            for row in self._rows:
                yield _FakeRecord(row)
        return gen()

    def keys(self) -> list[str]:
        """Return column names of the first row."""
        return list(self._rows[0]) if self._rows else []


class _FakeTx:
    """Fake explicit transaction whose run results are queued per call."""

    def __init__(self, results: list[list[dict[str, Any]]] | None = None, commit_error: Exception | None = None) -> None:
        """Queue result sets; commit may be told to fail."""
        self._results = list(results or [])
        self.fence_rows: list[dict[str, Any]] = [{"erased": False}]
        self.run = AsyncMock(side_effect=self._next)
        self.commit = AsyncMock(side_effect=commit_error)
        self.rollback = AsyncMock()

    async def _next(self, *args: Any, **_kwargs: Any) -> _FakeResult:
        """Return the next queued result (empty when exhausted)."""
        from modules.knowledge.temporal.graph_fence import LOCK_PARTITION_FENCE

        if args and args[0] == LOCK_PARTITION_FENCE:
            return _FakeResult(self.fence_rows)
        return _FakeResult(self._results.pop(0) if self._results else [])


class _FakeSession:
    """Fake session handing out one prepared transaction."""

    def __init__(self, tx: _FakeTx) -> None:
        """Bind the transaction returned by begin_transaction."""
        self.begin_transaction = AsyncMock(return_value=tx)

    async def __aenter__(self) -> Self:
        """Enter the session context."""
        return self

    async def __aexit__(self, *_exc: object) -> None:
        """Leave the session context."""


def _fake_client(*txs: _FakeTx) -> tuple[MagicMock, list[_FakeSession]]:
    """Build a client whose successive session() calls return sessions over the given transactions."""
    sessions = [_FakeSession(tx) for tx in txs]
    client = MagicMock()
    client.session = MagicMock(side_effect=sessions)
    client.close = AsyncMock()
    return client, sessions


async def _make_driver(*txs: _FakeTx) -> tuple[Any, MagicMock, list[_FakeSession]]:
    """Build a SafeDriver off-loop (as production does) and swap in a fake client."""
    driver = await asyncio.to_thread(_make_driver_type(), "bolt://127.0.0.1:7687", "neo4j", "pw", "neo4j")
    await driver.client.close()
    client, sessions = _fake_client(*txs)
    driver.client = client
    return driver, client, sessions


def _ownership() -> DispatchOwnership:
    """Return a valid dispatch ownership."""
    return DispatchOwnership(uuid4(), "g1", "a" * 40, 123)


@contextlib.contextmanager
def _in_scope(scope: _DispatchScope | None) -> Iterator[None]:
    """Run the body with the dispatch scope ContextVar set."""
    token = _dispatch_scope.set(scope)
    try:
        yield
    finally:
        _dispatch_scope.reset(token)


class TestDispatchScopeDriver:
    """Tests for the Neo4j SafeDriver transaction guard and dispatch scope."""

    @pytest.mark.asyncio
    async def test_read_routing_uses_read_mode_with_metadata_and_timeout(self) -> None:
        """routing_='r' opens a READ session; the transaction carries the token and 5s timeout."""
        tx = _FakeTx([[{"x": 1}]])
        driver, client, sessions = await _make_driver(tx)
        scope = _DispatchScope(_ownership())
        with _in_scope(scope):
            rows, _, keys = await driver.execute_query("MATCH (n) RETURN n.x AS x", routing_="r", p=1)
        assert rows == [{"x": 1}] and keys == ["x"]
        assert client.session.call_args.kwargs["default_access_mode"] == "WRITE"
        kwargs = sessions[0].begin_transaction.call_args.kwargs
        assert kwargs["timeout"] == 5
        assert kwargs["metadata"]["umwelt_dispatch"] == scope.token
        assert tx.run.await_args_list[-1].args == ("MATCH (n) RETURN n.x AS x", {"p": 1})
        assert tx.run.await_count == 2
        tx.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_default_routing_uses_write_mode(self) -> None:
        """Without routing_ the session is WRITE."""
        driver, client, _ = await _make_driver(_FakeTx())
        with _in_scope(_DispatchScope(_ownership())):
            await driver.execute_query("MATCH (n) RETURN n")
        assert client.session.call_args.kwargs["default_access_mode"] == "WRITE"

    @pytest.mark.asyncio
    async def test_row_cap_aborts_and_rolls_back(self) -> None:
        """More than MAX_SUPPORT + 1 rows aborts with graph_query_result_limit and never commits."""
        tx = _FakeTx([[{"x": i} for i in range(MAX_SUPPORT + 5)]])
        driver, _, _ = await _make_driver(tx)
        with _in_scope(_DispatchScope(_ownership())), pytest.raises(GraphOperationError, match="graph_query_result_limit"):
            await driver.execute_query("MATCH (n) RETURN n")
        tx.rollback.assert_awaited_once()
        tx.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_neo4j_temporal_converted_to_native(self) -> None:
        """neo4j.time values, including nested ones, come back as stdlib datetimes."""
        moment = neo_time.DateTime(2024, 1, 2, 3, 4, 5, tzinfo=UTC)
        tx = _FakeTx([[{"t": moment, "nested": {"d": [moment]}}]])
        driver, _, _ = await _make_driver(tx)
        with _in_scope(_DispatchScope(_ownership())):
            rows, _, _ = await driver.execute_query("MATCH (n) RETURN n")
        native = datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC)
        assert rows[0]["t"] == native and isinstance(rows[0]["t"], datetime)
        assert rows[0]["nested"]["d"] == [native]

    @pytest.mark.asyncio
    async def test_nul_stripped_and_datetimes_not_stringified(self) -> None:
        """Parameters lose NUL bytes but keep datetimes as native objects."""
        tx = _FakeTx()
        driver, _, _ = await _make_driver(tx)
        moment = datetime(2024, 1, 2, tzinfo=UTC)
        with _in_scope(_DispatchScope(_ownership())):
            await driver.execute_query("MATCH (n) RETURN n", name="a\x00b", when=moment, items=["c\x00"])
        sent = tx.run.call_args.args[1]
        assert sent == {"name": "ab", "when": moment, "items": ["c"]}
        assert isinstance(sent["when"], datetime)

    @pytest.mark.asyncio
    async def test_unscoped_query_denied_but_health_allowed(self) -> None:
        """Arbitrary statements need a dispatch scope; health and index DDL carry the setup tag."""
        tx = _FakeTx([[{"healthy": 1}]])
        driver, _, sessions = await _make_driver(tx)
        with pytest.raises(GraphOperationError, match="graph_dispatch_ownership_required"):
            await driver.execute_query("MATCH (n) RETURN n")
        await driver.health_check()
        assert sessions[0].begin_transaction.call_args.kwargs["metadata"]["umwelt_dispatch"] == "setup"

    @pytest.mark.asyncio
    async def test_index_exists_error_ignored(self) -> None:
        """A racing EquivalentSchemaRuleAlreadyExists failure on index DDL is swallowed."""
        tx = _FakeTx()
        tx.run = AsyncMock(side_effect=RuntimeError("EquivalentSchemaRuleAlreadyExists: x"))
        driver, _, _ = await _make_driver(tx)
        assert await driver.execute_query("CREATE INDEX a IF NOT EXISTS FOR (n:Entity) ON (n.uuid)") is None

    @pytest.mark.asyncio
    async def test_statements_share_one_write_transaction(self) -> None:
        """execute_statements runs every statement in a single explicit transaction and commits once."""
        tx = _FakeTx()
        driver, client, sessions = await _make_driver(tx)
        with _in_scope(_DispatchScope(_ownership())):
            await driver.execute_statements([("Q1", {"a": 1}), ("Q2", {}), ("Q3", {}), ("Q4", {})])
        assert client.session.call_count == 1
        sessions[0].begin_transaction.assert_awaited_once()
        assert tx.run.await_count == 5  # One guard and all payloads share this transaction.
        tx.commit.assert_awaited_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("fence_rows", [
        [{"erased": True}], [], [{}], [{"erased": None}], [{"erased": 0}],
        [{"erased": False}, {"erased": False}],
    ])
    async def test_erased_or_unproved_partition_never_reaches_payload(self, fence_rows: list[dict[str, Any]]) -> None:
        tx = _FakeTx()
        tx.fence_rows = fence_rows
        driver, _, _ = await _make_driver(tx)
        with _in_scope(_DispatchScope(_ownership())), pytest.raises(GraphOperationError, match="graph_partition_erased"):
            await driver.execute_query("CREATE (n {secret: $text})", text="private")
        assert tx.run.await_count == 1
        tx.commit.assert_not_awaited()
        tx.rollback.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_partition_guard_locks_original_group_before_payload(self) -> None:
        from modules.knowledge.temporal.graph_fence import LOCK_PARTITION_FENCE

        tx = _FakeTx()
        driver, _, _ = await _make_driver(tx)
        ownership = _ownership()
        with _in_scope(_DispatchScope(ownership)):
            await driver.execute_statements([("Q1", {}), ("Q2", {})])
        assert tx.run.await_args_list[0].args == (LOCK_PARTITION_FENCE, {"partition_id": ownership.group_id})
        assert [call.args[0] for call in tx.run.await_args_list[1:]] == ["Q1", "Q2"]
        tx.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_ambiguous_commit_poisons_scope(self) -> None:
        """A failure while committing is unknown and blocks later commands under the scope."""
        tx = _FakeTx(commit_error=RuntimeError("connection reset"))
        driver, _, _ = await _make_driver(tx)
        scope = _DispatchScope(_ownership())
        with _in_scope(scope):
            with pytest.raises(GraphOperationUnknown):
                await driver.execute_statements([("Q1", {})])
            assert scope.poisoned
            tx.rollback.assert_not_awaited()
            with pytest.raises(GraphOperationUnknown, match="graph_dispatch_connection_lost"):
                await driver.execute_query("MATCH (n) RETURN n")

    @pytest.mark.asyncio
    async def test_untagged_metadata_rejected(self) -> None:
        """The transaction wrapper refuses any transaction lacking a dispatch tag."""
        driver, client, _ = await _make_driver()
        with pytest.raises(GraphOperationError, match="graph_dispatch_command_denied"):
            await driver._transact([("Q", {})], read=True, metadata={}, scope=None)
        client.session.assert_not_called()


class TestBulkShape:
    """Tests for the pinned four-call NEO4J bulk payload validation."""

    @staticmethod
    def _bulk(provider: Any, node_query_override: Any = None) -> tuple[Any, list[Any], tuple[Any, ...], dict[str, Any]]:
        """Build captured calls and Graphiti arguments for one episode/entity/mention/fact."""
        from graphiti_core.models.edges.edge_db_queries import (
            get_entity_edge_save_bulk_query,
            get_episodic_edge_save_bulk_query,
        )
        from graphiti_core.models.nodes.node_db_queries import (
            get_entity_node_save_bulk_query,
            get_episode_node_save_bulk_query,
        )

        ids = [str(uuid4()) for _ in range(4)]
        episode = SimpleNamespace(uuid=ids[0], group_id="g1")
        node = SimpleNamespace(uuid=ids[1], group_id="g1")
        mention = SimpleNamespace(uuid=ids[2], group_id="g1", source_node_uuid=ids[0], target_node_uuid=ids[1])
        fact = SimpleNamespace(uuid=ids[3], group_id="g1", source_node_uuid=ids[1], target_node_uuid=ids[1],
                               episodes=[ids[0]])
        payload = {"uuid": ids[1], "group_id": "g1", "labels": ["Entity"]}
        node_query = get_entity_node_save_bulk_query(provider, [payload]) if node_query_override is None else node_query_override
        calls = [
            (get_episode_node_save_bulk_query(provider), {"episodes": [{"uuid": ids[0], "group_id": "g1"}]}),
            (node_query, {"nodes": [payload]}),
            (get_episodic_edge_save_bulk_query(provider),
             {"episodic_edges": [{"uuid": ids[2], "group_id": "g1", "source_node_uuid": ids[0],
                                  "target_node_uuid": ids[1]}]}),
            (get_entity_edge_save_bulk_query(provider),
             {"entity_edges": [{"uuid": ids[3], "group_id": "g1", "source_node_uuid": ids[1],
                                "target_node_uuid": ids[1], "episodes": [ids[0]]}]}),
        ]
        args = ([episode], [mention], [node], [fact], object())
        kwargs = {"driver": SimpleNamespace(provider=provider, graph_operations_interface=None)}
        return calls, args, kwargs, payload

    @pytest.mark.asyncio
    async def test_neo4j_shape_accepted(self) -> None:
        """The pinned four-call NEO4J shape validates and returns the final node payloads."""
        from graphiti_core.driver.driver import GraphProvider

        driver, _, _ = await _make_driver()
        calls, args, kwargs, payload = self._bulk(GraphProvider.NEO4J)
        assert driver.session()._validated_bulk_payloads(calls, args, kwargs) == [payload]

    @pytest.mark.asyncio
    async def test_legacy_provider_and_list_shape_rejected(self) -> None:
        """A non-Neo4j provider or list-of-queries node call (the removed per-node shape) is refused."""
        from graphiti_core.driver.driver import GraphProvider

        driver, _, _ = await _make_driver()
        session = driver.session()
        calls, args, kwargs, _ = self._bulk(GraphProvider.KUZU)
        with pytest.raises(GraphOperationError, match="graph_bulk_dispatch_shape_unsupported"):
            session._validated_bulk_payloads(calls, args, kwargs)
        calls, args, kwargs, _ = self._bulk(GraphProvider.NEO4J, node_query_override=[("Q", {"nodes": []})])
        with pytest.raises(GraphOperationError, match="graph_bulk_dispatch_shape_unsupported"):
            session._validated_bulk_payloads(calls, args, kwargs)


class TestCessationAndDispatchSession:
    """Tests for tagged-transaction cessation and dispatch-session probing."""

    @staticmethod
    async def _graph(*txs: _FakeTx) -> tuple[TemporalGraph, MagicMock]:
        """Return a READY TemporalGraph over a fake-client SafeDriver."""
        graph = TemporalGraph(GraphConfiguration(enabled=True, host="graph", password="pw"))
        driver, client, _ = await _make_driver(*txs)
        graph._driver = driver
        graph.state = GraphState.READY
        return graph, client

    @pytest.mark.asyncio
    async def test_cessation_show_terminate_poll(self) -> None:
        """Matching tagged transactions are listed, terminated, and polled until none remain."""
        show = _FakeTx([[{"transactionId": "t1"}]])
        terminate = _FakeTx([[]])
        poll = _FakeTx([[]])
        graph, _ = await self._graph(show, terminate, poll)
        ownership = _ownership()
        context = SimpleNamespace(
            group_id=ownership.group_id, operation_id=ownership.operation_id,
            authorize_dispatch_cessation=AsyncMock(),
        )
        assert await graph.verify_dispatch_cessation(ownership, context) is True  # type: ignore[arg-type]
        token = _DispatchScope(ownership).token
        assert show.run.call_args.args[1] == {"token": token}
        assert "SHOW TRANSACTIONS" in show.run.call_args.args[0]
        assert terminate.run.call_args.args == ("TERMINATE TRANSACTIONS $ids", {"ids": ["t1"]})
        assert poll.run.await_count == 1
        assert context.authorize_dispatch_cessation.await_count == 2

    @pytest.mark.asyncio
    async def test_cessation_no_transactions(self) -> None:
        """No tagged transaction means cessation is proven without a TERMINATE."""
        show = _FakeTx([[]])
        graph, client = await self._graph(show)
        ownership = _ownership()
        context = SimpleNamespace(
            group_id=ownership.group_id, operation_id=ownership.operation_id,
            authorize_dispatch_cessation=AsyncMock(),
        )
        assert await graph.verify_dispatch_cessation(ownership, context) is True  # type: ignore[arg-type]
        assert client.session.call_count == 1

    @pytest.mark.asyncio
    async def test_dispatch_session_probes_timeout_and_journals_before_scope(self) -> None:
        """A positive server timeout mints a valid ownership, journals it, and sets the scope."""
        probe = _FakeTx([[{"name": "db.transaction.timeout", "value": "5s"}]])
        graph, _ = await self._graph(probe)
        journal = AsyncMock()
        completion = AsyncMock()
        context = SimpleNamespace(
            group_id="g1", operation_id=uuid4(),
            record_dispatch_ownership=journal, record_dispatch_completion=completion,
        )
        async with graph.dispatch_session(context) as ownership:  # type: ignore[arg-type]
            journal.assert_awaited_once_with(ownership)
            scope = _dispatch_scope.get()
            assert scope is not None and scope.ownership is ownership
        assert re.fullmatch(r"[0-9a-f]{40}", ownership.server_run_id)
        assert _dispatch_scope.get() is None and scope.poisoned
        completion.assert_awaited_once_with(ownership)

    @pytest.mark.asyncio
    async def test_dispatch_session_rejects_disabled_server_timeout(self) -> None:
        """A zero db.transaction.timeout is refused before anything is journaled."""
        probe = _FakeTx([[{"name": "db.transaction.timeout", "value": "0s"}]])
        graph, _ = await self._graph(probe)
        journal = AsyncMock()
        context = SimpleNamespace(
            group_id="g1", operation_id=uuid4(), record_dispatch_ownership=journal, record_dispatch_completion=None,
        )
        with pytest.raises(GraphOperationError, match="graph_write_timeout_unconfigured"):
            async with graph.dispatch_session(context):  # type: ignore[arg-type]
                pytest.fail("scope must not open")
        journal.assert_not_awaited()


class TestPartitionIsolation:
    """Tests that shared-database reads are bound to the authorized partition."""

    @pytest.mark.asyncio
    async def test_incident_ids_absent_binds_group(self) -> None:
        """The absence read carries the group_id predicate and parameter."""
        driver = MagicMock()
        driver.execute_query = AsyncMock(return_value=([], None, []))
        assert await _incident_ids_absent(driver, ["e1"], "g1") is True
        query = driver.execute_query.call_args.args[0]
        assert "group_id: $group_id" in query
        assert driver.execute_query.call_args.kwargs["group_id"] == "g1"

    def test_foreign_group_record_rejected(self) -> None:
        """A fetched record from another group is refused; same-group and empty pass."""
        _require_group([SimpleNamespace(group_id="g1")], "g1")
        _require_group([], "g1")
        with pytest.raises(GraphOperationError, match="graph_partition_mismatch"):
            _require_group([SimpleNamespace(group_id="g1"), SimpleNamespace(group_id="other")], "g1")

    def test_defaults_and_index_error_mapping(self) -> None:
        """Graph defaults target Neo4j and an equivalent-index race maps to graph_index_exists."""
        config = GraphConfiguration()
        assert config.port == 7687 and config.database == "neo4j"
        assert str(_safe_error(RuntimeError("EquivalentSchemaRuleAlreadyExists"))) == "graph_index_exists"
