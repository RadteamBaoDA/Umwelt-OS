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
- TemporalGraph adapter lifecycle (init, initialize, health, close) and _OwnedDispatchTransport.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from modules.knowledge.temporal.adapter import (
    CandidateNodeReplacement,
    CanonicalEntityBinding,
    CanonicalNodeRecoveryAction,
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
    _entity_state_fingerprint,
    _episode_state_fingerprint,
    _fact_state,
    _normalized_graph_timestamp,
    _OwnedDispatchTransport,
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
            enabled=False, host="localhost", port=6379, username=None, password="secret",
            database="test_db",
        )
        adapter = TemporalGraph(config)
        state = await adapter.initialize()
        assert state == GraphState.DISABLED

    @pytest.mark.asyncio
    async def test_initialize_missing_credentials_returns_unconfigured(self) -> None:
        """Verify initialize returns UNCONFIGURED when host or password missing."""
        config = GraphConfiguration(
            enabled=True, host="", port=6379, username=None, password="secret",
            database="test_db",
        )
        adapter = TemporalGraph(config)
        state = await adapter.initialize()
        assert state == GraphState.UNCONFIGURED

    @pytest.mark.asyncio
    async def test_health_checks(self) -> None:
        """Verify health checks report appropriate states."""
        # Disabled
        config_disabled = GraphConfiguration(
            enabled=False, host="localhost", port=6379, username=None, password="secret",
            database="test_db",
        )
        adapter_disabled = TemporalGraph(config_disabled)
        assert await adapter_disabled.health() == GraphState.DISABLED

        # Unconfigured
        config_unconf = GraphConfiguration(
            enabled=True, host="", port=6379, username=None, password=None,
            database="test_db",
        )
        adapter_unconf = TemporalGraph(config_unconf)
        assert await adapter_unconf.health() == GraphState.UNCONFIGURED

    @pytest.mark.asyncio
    async def test_close_resets_state(self) -> None:
        """Verify close resets adapter state to unconfigured or disabled."""
        config = GraphConfiguration(
            enabled=True, host="localhost", port=6379, username=None, password="secret",
            database="test_db",
        )
        adapter = TemporalGraph(config)
        adapter.state = GraphState.READY
        adapter._driver = AsyncMock()

        await adapter.close()
        assert adapter.state == GraphState.UNCONFIGURED
        assert adapter._driver is None


class TestOwnedDispatchTransport:
    """Tests for _OwnedDispatchTransport Redis MULTI/EXEC dispatch and error handling."""

    @pytest.fixture
    def mock_ownership(self) -> MagicMock:
        """Fixture for DispatchOwnership."""
        ownership = MagicMock()
        ownership.group_id = "test_group"
        return ownership

    @pytest.mark.asyncio
    async def test_command_denied_for_non_graph_query(self, mock_ownership: MagicMock) -> None:
        """Verify commands other than GRAPH.QUERY and GRAPH.RO_QUERY are rejected."""
        conn = AsyncMock()
        conn.is_connected = True
        transport = _OwnedDispatchTransport(conn, mock_ownership)

        with pytest.raises(GraphOperationError, match="graph_dispatch_command_denied"):
            await transport.command("SET", "key", "val")

    @pytest.mark.asyncio
    async def test_command_denied_for_group_id_mismatch(self, mock_ownership: MagicMock) -> None:
        """Verify command with mismatched group_id is denied."""
        conn = AsyncMock()
        conn.is_connected = True
        transport = _OwnedDispatchTransport(conn, mock_ownership)

        with pytest.raises(GraphOperationError, match="graph_dispatch_command_denied"):
            await transport.command("GRAPH.QUERY", "wrong_group", "MATCH (n) RETURN n")

    @pytest.mark.asyncio
    async def test_command_successful_multi_exec(self, mock_ownership: MagicMock) -> None:
        """Verify successful MULTI/EXEC sequence returns the execution result."""
        conn = AsyncMock()
        conn.is_connected = True
        conn.send_command = AsyncMock()
        conn.read_response = AsyncMock(side_effect=["OK", "QUEUED", [["node_result"]]])

        transport = _OwnedDispatchTransport(conn, mock_ownership)
        result = await transport.command("GRAPH.QUERY", "test_group", "MATCH (n) RETURN n")

        assert result == ["node_result"]
        assert not transport.poisoned

    @pytest.mark.asyncio
    async def test_transport_poisoned_on_exception(self, mock_ownership: MagicMock) -> None:
        """Verify transport is poisoned when communication fails during execution."""
        conn = AsyncMock()
        conn.is_connected = True
        conn.send_command = AsyncMock()
        conn.read_response = AsyncMock(side_effect=["OK", RuntimeError("Socket closed")])

        transport = _OwnedDispatchTransport(conn, mock_ownership)
        with pytest.raises(RuntimeError, match="Socket closed"):
            await transport.command("GRAPH.QUERY", "test_group", "MATCH (n) RETURN n")

        assert transport.poisoned

        # Subsequent commands fail with connection lost
        with pytest.raises(GraphOperationUnknown, match="graph_dispatch_connection_lost"):
            await transport.command("GRAPH.QUERY", "test_group", "MATCH (n) RETURN n")
