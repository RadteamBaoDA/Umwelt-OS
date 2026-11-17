"""Characterization tests for TemporalGraph.reconcile_canonical_nodes and reconcile_fact_recovery.

Both run against the in-memory fake graph from ``fake_graph`` and pin the
observable contract: request validation, owner authorization before and after the
intent journal, intent-before-mutation ordering, per-action classification and the
unknown outcome on timeout.
"""

from __future__ import annotations

import contextlib
import hashlib
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from modules.knowledge.temporal.adapter import (
    CandidateNodeReplacement,
    CanonicalNodeRecoveryAction,
    ExactFactRecoveryAction,
    ExactFactReplacement,
    ExactFactState,
    ExactFactSupport,
    ExactGraphLink,
    GraphConfiguration,
    GraphOperationError,
    GraphState,
    GraphWriteReceipt,
    OperationAuthorization,
    TemporalGraph,
    _entity_state_fingerprint,
    _fact_recovery_fingerprint,
    _fact_state,
)

from .fake_graph import (
    DIMS,
    GROUP,
    NOW,
    FakeDriver,
    FakeGraph,
    install_fake_graphiti,
    make_edge,
    make_entity,
)

ABSENT = hashlib.sha256(b"absent").hexdigest()


class Harness:
    """Shared adapter, fake graph and owner closure for reconcile tests."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.graph = FakeGraph()
        install_fake_graphiti(monkeypatch, self.graph)
        self.episode_id = str(uuid4())
        self.other_episode = str(uuid4())
        self.operation_id = uuid4()
        self.lease = uuid4()
        self.journal: list[GraphWriteReceipt] = []
        self.authorize_node = AsyncMock()
        self.authorize_fact = AsyncMock()
        self.adapter = TemporalGraph(GraphConfiguration(enabled=True, host="h"))
        self.adapter.state = GraphState.READY
        self.adapter._driver = object()
        monkeypatch.setattr(
            self.adapter, "_partition_driver", AsyncMock(return_value=FakeDriver(self.graph))
        )
        monkeypatch.setattr(self.adapter, "_validate_partition", AsyncMock())

    def common(self) -> dict[str, Any]:
        """Receipt fields shared by every receipt of this operation."""
        return {
            "operation_id": self.operation_id,
            "lease_token": self.lease,
            "episode_id": UUID(self.episode_id),
            "group_id": GROUP,
            "mapping_revision": 0,
            "desired_support_digest": "a" * 64,
        }

    def context(self, **overrides: Any) -> OperationAuthorization:
        """Build the owner closure; journaling and fence entry are logged on the graph."""
        graph = self.graph

        @contextlib.asynccontextmanager
        async def fence() -> AsyncIterator[None]:
            graph.events.append(("fence_enter",))
            try:
                yield
            finally:
                graph.events.append(("fence_exit",))

        async def record(receipt: GraphWriteReceipt) -> None:
            graph.events.append(("intent", receipt.phase))
            self.journal.append(receipt)

        values: dict[str, Any] = {
            "group_id": GROUP,
            "source_id": uuid4(),
            "source_generation": 1,
            "operation_id": self.operation_id,
            "lease_token": self.lease,
            "evidence": (),
            "fence": fence,
            "authorize": AsyncMock(),
            "canonicalize": AsyncMock(),
            "validate_partition": AsyncMock(),
            "record_write_intent": record,
            "reasoning": SimpleNamespace(),
            "small_reasoning": SimpleNamespace(),
            "embedding": SimpleNamespace(dimensions=DIMS),
            "partition_episode_ids": (self.episode_id, self.other_episode),
            "partition_inventory_complete": True,
            "authorize_node_recovery": self.authorize_node,
            "authorize_fact_recovery": self.authorize_fact,
        }
        values.update(overrides)
        return OperationAuthorization(**values)

    def mutations(self) -> list[tuple[Any, ...]]:
        """Return only the graph-mutating events in order."""
        return [
            e
            for e in self.graph.events
            if e[0]
            in {"delete_entity", "save_entity", "clear_embedding", "save_fact", "delete_fact"}
        ]


# --------------------------------------------------------------------------- nodes


class NodeHarness(Harness):
    """One entity node N with a bulk receipt that authorizes it."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, present: bool = True) -> None:
        super().__init__(monkeypatch)
        self.node_id = str(uuid4())
        self.fingerprint: str | None = None
        if present:
            self.graph.entities[self.node_id] = make_entity(self.node_id)
            self.fingerprint = _entity_state_fingerprint(self.graph.entities[self.node_id], DIMS)

    def receipts(self) -> tuple[GraphWriteReceipt, ...]:
        """Return a bulk receipt naming N as a journaled node write."""
        return (
            GraphWriteReceipt(
                phase="bulk_write_intent",
                episode_state_fingerprint="c" * 64,
                prior_episode_state_fingerprint="c" * 64,
                entity_ids=(self.node_id,),
                prior_entity_state_fingerprints=(
                    (self.node_id, self.fingerprint is not None, self.fingerprint),
                ),
                intended_entity_state_fingerprints=((self.node_id, "b" * 64),),
                **self.common(),
            ),
        )

    def action(self, kind: str = "retain", **overrides: Any) -> CanonicalNodeRecoveryAction:
        """Build an action for N with the current fingerprint unless overridden."""
        values: dict[str, Any] = {
            "graph_entity_uuid": self.node_id,
            "expected_current_state_fingerprint": self.fingerprint,
            "action": kind,
        }
        values.update(overrides)
        return CanonicalNodeRecoveryAction(**values)

    async def run(self, action: CanonicalNodeRecoveryAction, **ctx: Any) -> Any:
        """Reconcile one action with the default receipts."""
        return await self.adapter.reconcile_canonical_nodes(
            self.receipts(), (action,), self.context(**ctx)
        )


@pytest.mark.asyncio
async def test_node_recovery_requires_ready_graph(monkeypatch: pytest.MonkeyPatch) -> None:
    """A graph that is not READY refuses node recovery outright."""
    harness = NodeHarness(monkeypatch)
    harness.adapter.state = GraphState.UNAVAILABLE
    with pytest.raises(GraphOperationError, match="graph_node_recovery_unavailable"):
        await harness.run(harness.action())


@pytest.mark.asyncio
async def test_node_recovery_rejects_unjournaled_node(monkeypatch: pytest.MonkeyPatch) -> None:
    """An action for a node no receipt authorizes is rejected before the fence."""
    harness = NodeHarness(monkeypatch)
    foreign = CanonicalNodeRecoveryAction(
        graph_entity_uuid=str(uuid4()), expected_current_state_fingerprint=None, action="retain"
    )
    with pytest.raises(
        GraphOperationError, match="graph_node_recovery_outside_authorized_partition"
    ):
        await harness.adapter.reconcile_canonical_nodes(
            harness.receipts(), (foreign,), harness.context()
        )
    assert harness.graph.events == []


@pytest.mark.asyncio
async def test_node_retain_converges_without_journal(monkeypatch: pytest.MonkeyPatch) -> None:
    """A matching retained node converges without a new intent."""
    harness = NodeHarness(monkeypatch)
    outcome = await harness.run(harness.action())
    assert outcome.converged and outcome.retained_ids == (harness.node_id,)
    assert outcome.reason == "converged" and harness.journal == []
    assert harness.authorize_node.await_count == 1
    assert harness.graph.events[0] == ("fence_enter",) and harness.graph.events[-1] == (
        "fence_exit",
    )


@pytest.mark.asyncio
async def test_node_incident_link_mismatch_is_unresolved(monkeypatch: pytest.MonkeyPatch) -> None:
    """Live incident links that differ from the authorized inventory leave the node unresolved."""
    harness = NodeHarness(monkeypatch)
    harness.graph.node_links[harness.node_id] = [
        {
            "edge_id": str(uuid4()),
            "relationship_type": "REL",
            "source_node_id": str(uuid4()),
            "target_node_id": harness.node_id,
            "episode_ids": [],
        }
    ]
    outcome = await harness.run(harness.action())
    assert not outcome.converged and outcome.unresolved_ids == (harness.node_id,)
    assert outcome.reason == "exact_node_state_unresolved"


@pytest.mark.asyncio
async def test_node_fingerprint_mismatch_is_unresolved(monkeypatch: pytest.MonkeyPatch) -> None:
    """A node whose state drifted from the expected fingerprint stays unresolved."""
    harness = NodeHarness(monkeypatch)
    outcome = await harness.run(harness.action(expected_current_state_fingerprint="d" * 64))
    assert outcome.unresolved_ids == (harness.node_id,) and harness.mutations() == []


@pytest.mark.asyncio
async def test_node_delete_orphan_journals_before_delete(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deleting an orphan journals intent, reauthorizes, rechecks, deletes and reads back absence."""
    harness = NodeHarness(monkeypatch)
    outcome = await harness.run(harness.action("delete_orphan"))
    events = harness.graph.events
    assert outcome.converged and outcome.deleted_ids == (harness.node_id,)
    assert events.index(("intent", "cleanup_write_intent")) < events.index(
        ("delete_entity", (harness.node_id,))
    )
    assert harness.authorize_node.await_count == 2
    assert len(harness.authorize_node.await_args_list[1].args[0]) == 2
    assert harness.node_id not in harness.graph.entities
    assert harness.journal[0].intended_entity_state_fingerprints == ((harness.node_id, None),)


@pytest.mark.asyncio
async def test_node_delete_failure_is_unresolved(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failing delete is swallowed per action and reported unresolved."""
    harness = NodeHarness(monkeypatch)
    harness.graph.fail_entity_delete = True
    outcome = await harness.run(harness.action("delete_orphan"))
    assert not outcome.converged and outcome.unresolved_ids == (harness.node_id,)
    assert outcome.deleted_ids == ()


@pytest.mark.asyncio
async def test_node_absent_never_written_is_unwritten(monkeypatch: pytest.MonkeyPatch) -> None:
    """A node that never existed and is absent is reported unwritten for a retain."""
    harness = NodeHarness(monkeypatch, present=False)
    outcome = await harness.run(harness.action())
    assert outcome.unwritten_ids == (harness.node_id,) and outcome.converged


@pytest.mark.asyncio
async def test_node_absent_delete_with_unwritten_prior_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An absent node whose prior journaled state was absent is unwritten for delete_orphan."""
    harness = NodeHarness(monkeypatch, present=False)
    outcome = await harness.run(harness.action("delete_orphan"))
    assert outcome.unwritten_ids == (harness.node_id,) and harness.journal == []


@pytest.mark.asyncio
async def test_node_replace_from_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replacement rewrites the node, drops its vector and verifies the read-back fingerprint."""
    harness = NodeHarness(monkeypatch)
    candidate = CandidateNodeReplacement(
        graph_entity_uuid=harness.node_id,
        group_id=GROUP,
        node_name="new name",
        node_summary="",
        support_episode_ids=(harness.other_episode,),
        field_support=(
            ("name", hashlib.sha256(b"new name").hexdigest(), (harness.other_episode,)),
        ),
    )
    action = harness.action("replace_from_current_support", replacement_candidate=candidate)
    outcome = await harness.run(action)
    events = harness.graph.events
    assert outcome.converged and outcome.replaced_ids == (harness.node_id,)
    assert events.index(("intent", "cleanup_write_intent")) < events.index(
        ("save_entity", harness.node_id)
    )
    assert events.index(("save_entity", harness.node_id)) < events.index(
        ("clear_embedding", harness.node_id)
    )
    assert harness.graph.entities[harness.node_id].name == "new name"
    assert harness.journal[0].candidate_field_support[0][1] == "name"


@pytest.mark.asyncio
async def test_node_candidate_naming_current_episode_is_unresolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A replacement candidate still supported by the deleted episode is refused."""
    harness = NodeHarness(monkeypatch)
    candidate = CandidateNodeReplacement(
        graph_entity_uuid=harness.node_id,
        group_id=GROUP,
        node_name="x",
        node_summary="",
        support_episode_ids=(harness.episode_id,),
        field_support=(("name", hashlib.sha256(b"x").hexdigest(), (harness.episode_id,)),),
    )
    outcome = await harness.run(
        harness.action("replace_from_current_support", replacement_candidate=candidate)
    )
    assert outcome.unresolved_ids == (harness.node_id,) and harness.mutations() == []


@pytest.mark.asyncio
async def test_node_timeout_returns_unknown_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    """A timeout inside the window yields the unknown outcome with every action unresolved."""
    harness = NodeHarness(monkeypatch)
    harness.authorize_node.side_effect = TimeoutError()
    outcome = await harness.run(harness.action())
    assert not outcome.converged and outcome.unresolved_ids == (harness.node_id,)
    assert outcome.reason == "graph_node_recovery_outcome_unknown"


@pytest.mark.asyncio
async def test_node_unexpected_error_returns_unknown_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unexpected dependency failure degrades to the unknown outcome."""
    harness = NodeHarness(monkeypatch)
    harness.authorize_node.side_effect = RuntimeError("boom")
    outcome = await harness.run(harness.action())
    assert outcome.reason == "graph_node_recovery_outcome_unknown"


@pytest.mark.asyncio
async def test_node_graph_operation_error_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """GraphOperationError from the owner callback is re-raised, not swallowed."""
    harness = NodeHarness(monkeypatch)
    harness.authorize_node.side_effect = GraphOperationError("owner_denied")
    with pytest.raises(GraphOperationError, match="owner_denied"):
        await harness.run(harness.action())


@pytest.mark.asyncio
async def test_node_delete_for_rebuild_requires_absent_incident_edges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rebuild deletion with an absent node is unresolved while a formerly incident edge is still present."""
    harness = NodeHarness(monkeypatch, present=False)
    edge_id = str(uuid4())
    link = ExactGraphLink(edge_id, "REL", str(uuid4()), harness.node_id, ())
    harness.graph.edges[edge_id] = make_edge(edge_id, str(uuid4()), harness.node_id, [])
    harness.graph.entities.pop(harness.node_id, None)
    receipt = GraphWriteReceipt(
        phase="cleanup_write_intent",
        prior_episode_state_fingerprint="c" * 64,
        entity_ids=(harness.node_id,),
        incident_links=(link,),
        prior_entity_state_fingerprints=((harness.node_id, True, "e" * 64),),
        intended_entity_state_fingerprints=((harness.node_id, None),),
        **harness.common(),
    )
    bulk = harness.receipts()[0]
    bulk = GraphWriteReceipt(
        phase="bulk_write_intent",
        episode_state_fingerprint="c" * 64,
        prior_episode_state_fingerprint="c" * 64,
        entity_ids=(harness.node_id,),
        prior_entity_state_fingerprints=((harness.node_id, True, "e" * 64),),
        intended_entity_state_fingerprints=((harness.node_id, "b" * 64),),
        **harness.common(),
    )
    action = CanonicalNodeRecoveryAction(
        graph_entity_uuid=harness.node_id,
        expected_current_state_fingerprint=None,
        action="delete_for_rebuild",
        expected_incident_links=(),
        rebuild_mapping_ids=(uuid4(),),
    )
    outcome = await harness.adapter.reconcile_canonical_nodes(
        (bulk, receipt), (action,), harness.context()
    )
    assert outcome.unresolved_ids == (harness.node_id,) and outcome.deleted_ids == ()


# --------------------------------------------------------------------------- facts


class FactHarness(Harness):
    """One fact edge F between two present endpoint nodes, journaled by a bulk receipt."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, present: bool = True) -> None:
        super().__init__(monkeypatch)
        self.fact_id = str(uuid4())
        self.source = str(uuid4())
        self.target = str(uuid4())
        for node_id in (self.source, self.target):
            self.graph.entities[node_id] = make_entity(node_id)
        self.edge = make_edge(
            self.fact_id, self.source, self.target, [self.episode_id, self.other_episode]
        )
        if present:
            self.graph.edges[self.fact_id] = self.edge
        self.state = _fact_state(self.edge, DIMS)

    def receipts(self, *, existed: bool = True) -> tuple[GraphWriteReceipt, ...]:
        """Return a bulk receipt journaling the fact's prior and intended state."""
        prior = ExactFactState(
            self.fact_id,
            self.source,
            self.target,
            self.state.episode_ids,
            self.state.state_fingerprint if existed else ABSENT,
            existed,
        )
        intended = ExactFactSupport(
            fact_id=self.fact_id,
            episode_ids=self.state.episode_ids,
            source_node_id=self.source,
            target_node_id=self.target,
            state_fingerprint=self.state.state_fingerprint,
        )
        return (
            GraphWriteReceipt(
                phase="bulk_write_intent",
                episode_state_fingerprint="c" * 64,
                prior_episode_state_fingerprint="c" * 64,
                fact_ids=(self.fact_id,),
                existing_fact_states=(prior,),
                intended_fact_support=(intended,),
                **self.common(),
            ),
        )

    def replacement(self) -> ExactFactReplacement:
        """Return a fresh snapshot supported only by the surviving episode."""
        return ExactFactReplacement(
            fact_id=self.fact_id,
            group_id=GROUP,
            source_node_id=self.source,
            target_node_id=self.target,
            name="REL",
            fact="fresh text",
            episode_ids=(self.other_episode,),
            created_at=NOW,
            reference_time=NOW,
            valid_at=None,
            invalid_at=None,
            expired_at=None,
            fact_embedding=(0.5, 0.25, 0.125),
        )

    def action(self, kind: str, **overrides: Any) -> ExactFactRecoveryAction:
        """Build an action expecting the fact's current fingerprint."""
        values: dict[str, Any] = {
            "fact_id": self.fact_id,
            "source_node_id": self.source,
            "target_node_id": self.target,
            "expected_current_state_fingerprint": _fact_recovery_fingerprint(self.edge, DIMS),
            "action": kind,
        }
        if kind == "replace_from_current_support":
            values["replacement"] = self.replacement()
        if kind == "delete_for_rebuild":
            values["rebuild_mapping_ids"] = (uuid4(),)
        values.update(overrides)
        return ExactFactRecoveryAction(**values)

    async def run(self, action: ExactFactRecoveryAction, receipts: Any = None) -> Any:
        """Reconcile one action."""
        return await self.adapter.reconcile_fact_recovery(
            receipts or self.receipts(), (action,), self.context()
        )


@pytest.mark.asyncio
async def test_fact_recovery_requires_ready_graph(monkeypatch: pytest.MonkeyPatch) -> None:
    """A graph that is not READY refuses fact recovery outright."""
    harness = FactHarness(monkeypatch)
    harness.adapter.state = GraphState.UNAVAILABLE
    with pytest.raises(GraphOperationError, match="graph_fact_recovery_unavailable"):
        await harness.run(harness.action("delete_unsupported"))


@pytest.mark.asyncio
async def test_fact_recovery_rejects_unjournaled_fact(monkeypatch: pytest.MonkeyPatch) -> None:
    """An action whose fact is not in the receipts is rejected before the fence."""
    harness = FactHarness(monkeypatch)
    other = str(uuid4())
    action = harness.action("delete_unsupported", fact_id=other)
    with pytest.raises(GraphOperationError, match="graph_fact_recovery_receipt_invalid"):
        await harness.run(action)
    assert harness.graph.events == []


@pytest.mark.asyncio
async def test_fact_replace_journals_before_save(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replacement journals intent, reauthorizes, rechecks endpoints, saves and verifies the read-back."""
    harness = FactHarness(monkeypatch)
    outcome = await harness.run(harness.action("replace_from_current_support"))
    events = harness.graph.events
    assert outcome.converged and outcome.replaced_ids == (harness.fact_id,)
    assert events.index(("intent", "cleanup_write_intent")) < events.index(
        ("save_fact", harness.fact_id)
    )
    assert harness.authorize_fact.await_count == 2
    assert harness.graph.edges[harness.fact_id].episodes == [harness.other_episode]
    assert harness.journal[0].fact_ids == (harness.fact_id,)


@pytest.mark.asyncio
async def test_fact_delete_unsupported_deletes_after_intent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unsupported facts are deleted after the intent and confirmed absent."""
    harness = FactHarness(monkeypatch)
    outcome = await harness.run(harness.action("delete_unsupported"))
    events = harness.graph.events
    assert outcome.deleted_ids == (harness.fact_id,) and outcome.converged
    assert events.index(("intent", "cleanup_write_intent")) < events.index(
        ("delete_fact", (harness.fact_id,))
    )
    assert harness.fact_id not in harness.graph.edges


@pytest.mark.asyncio
async def test_fact_already_at_intended_state_needs_no_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fact already equal to the replacement snapshot converges without journaling."""
    harness = FactHarness(monkeypatch)
    action = harness.action("replace_from_current_support")
    snapshot = action.replacement
    assert snapshot is not None
    harness.graph.edges[harness.fact_id] = make_edge(
        harness.fact_id, harness.source, harness.target, list(snapshot.episode_ids)
    )
    harness.graph.edges[harness.fact_id].fact = "fresh text"
    outcome = await harness.run(action)
    assert outcome.replaced_ids == (harness.fact_id,) and harness.journal == []
    assert harness.mutations() == []


@pytest.mark.asyncio
async def test_fact_fingerprint_mismatch_is_unresolved(monkeypatch: pytest.MonkeyPatch) -> None:
    """A live fact differing from the expected fingerprint is not modified."""
    harness = FactHarness(monkeypatch)
    outcome = await harness.run(
        harness.action("delete_unsupported", expected_current_state_fingerprint="d" * 64)
    )
    assert outcome.unresolved_ids == (harness.fact_id,) and harness.mutations() == []


@pytest.mark.asyncio
async def test_fact_missing_endpoint_is_unresolved(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing endpoint node leaves the fact unresolved before any journal write."""
    harness = FactHarness(monkeypatch)
    del harness.graph.entities[harness.target]
    outcome = await harness.run(harness.action("delete_unsupported"))
    assert outcome.unresolved_ids == (harness.fact_id,) and harness.journal == []


@pytest.mark.asyncio
async def test_fact_absent_with_initial_absence_receipt_is_deleted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An absent fact that never existed per the journal converges as deleted for delete_unsupported."""
    harness = FactHarness(monkeypatch, present=False)
    outcome = await harness.run(
        harness.action("delete_unsupported"), receipts=harness.receipts(existed=False)
    )
    assert outcome.deleted_ids == (harness.fact_id,) and harness.journal == []


@pytest.mark.asyncio
async def test_fact_absent_without_proof_is_unresolved(monkeypatch: pytest.MonkeyPatch) -> None:
    """An absent fact with no absence evidence and no cross-rebuild proof stays unresolved."""
    harness = FactHarness(monkeypatch, present=False)
    outcome = await harness.run(harness.action("delete_unsupported"))
    assert outcome.unresolved_ids == (harness.fact_id,)


@pytest.mark.asyncio
async def test_fact_timeout_returns_unknown_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    """A timeout inside the window yields the unknown outcome with every action unresolved."""
    harness = FactHarness(monkeypatch)
    harness.authorize_fact.side_effect = TimeoutError()
    outcome = await harness.run(harness.action("delete_unsupported"))
    assert not outcome.converged and outcome.unresolved_ids == (harness.fact_id,)
    assert outcome.reason == "graph_fact_recovery_outcome_unknown"


@pytest.mark.asyncio
async def test_fact_unexpected_error_returns_unknown_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unexpected dependency failure degrades to the unknown outcome."""
    harness = FactHarness(monkeypatch)
    harness.authorize_fact.side_effect = RuntimeError("boom")
    outcome = await harness.run(harness.action("delete_unsupported"))
    assert outcome.reason == "graph_fact_recovery_outcome_unknown"
