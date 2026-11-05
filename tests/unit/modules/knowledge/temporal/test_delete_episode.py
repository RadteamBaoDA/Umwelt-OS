"""Characterization tests for TemporalGraph.delete_episode against an in-memory fake graph.

The fake replaces the Graphiti node/edge classes and the Cypher driver so the
tests pin the observable contract: receipt validation, intent journaling before
any mutation, per-edge recheck -> write -> read-back ordering, support
subtraction (shared / removed / stale / pending), NodeNotFoundError handling and
the partial ``DeleteOutcome`` returned on failure.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from modules.knowledge.temporal.adapter import (
    CanonicalNodeRecoveryAction,
    ExactFactSupport,
    GraphConfiguration,
    GraphState,
    GraphWriteReceipt,
    OperationAuthorization,
    TemporalGraph,
    _entity_state_fingerprint,
    _episode_state_fingerprint,
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


class Scenario:
    """Wire graph, receipts, authorization and adapter for one delete case."""

    def __init__(
        self, monkeypatch: pytest.MonkeyPatch, *, with_entity: bool = False, with_shared: bool = True,
    ) -> None:
        self.graph = FakeGraph()
        self.episode_id = str(uuid4())
        self.other_episode = str(uuid4())
        self.fact_shared = str(uuid4())
        self.fact_solo = str(uuid4())
        self.mention_id = str(uuid4())
        self.entity_id = str(uuid4()) if with_entity else None
        self.source_node = str(uuid4())
        self.target_node = str(uuid4())
        graph = self.graph
        self.fact_ids = [self.fact_solo]
        if with_shared:
            graph.edges[self.fact_shared] = make_edge(
                self.fact_shared, self.source_node, self.target_node, [self.episode_id, self.other_episode])
            self.fact_ids = [self.fact_shared, self.fact_solo]
        graph.edges[self.fact_solo] = make_edge(
            self.fact_solo, self.source_node, self.target_node, [self.episode_id])
        graph.episodes[self.episode_id] = SimpleNamespace(
            uuid=self.episode_id, group_id=GROUP, name="ep", source=SimpleNamespace(value="text"),
            source_description="d", content="c", valid_at=NOW, created_at=NOW,
            entity_edges=list(self.fact_ids),
        )
        graph.mentions[self.mention_id] = SimpleNamespace(
            uuid=self.mention_id, group_id=GROUP, source_node_uuid=self.episode_id,
            target_node_uuid=self.source_node,
        )
        if with_entity:
            graph.entities[self.entity_id] = make_entity(self.entity_id)
        install_fake_graphiti(monkeypatch, graph)
        self.operation_id = uuid4()
        self.lease = uuid4()
        self.journal: list[GraphWriteReceipt] = []
        self.intent_error: Exception | None = None
        self.node_actions: tuple[CanonicalNodeRecoveryAction, ...] = ()
        self.authorize_node = AsyncMock()
        self.validate_partition = AsyncMock(
            side_effect=lambda *args: graph.events.append(("validate",) + args))
        self.adapter = TemporalGraph(GraphConfiguration(enabled=True, host="h"))
        self.adapter.state = GraphState.READY
        self.adapter._driver = object()
        driver = FakeDriver(graph)
        monkeypatch.setattr(self.adapter, "_partition_driver", AsyncMock(return_value=driver))
        monkeypatch.setattr(self.adapter, "_validate_partition", AsyncMock())

    def receipts(self, *, phase_override: dict[str, Any] | None = None) -> tuple[GraphWriteReceipt, ...]:
        """Build a valid shell + bulk receipt history matching the fake graph."""
        episode_fp = _episode_state_fingerprint(self.graph.episodes[self.episode_id])
        facts = [self.graph.edges[item] for item in self.fact_ids]
        states = tuple(_fact_state(edge, DIMS) for edge in facts)
        support = tuple(ExactFactSupport(
            fact_id=s.fact_id, episode_ids=s.episode_ids, source_node_id=s.source_node_id,
            target_node_id=s.target_node_id, state_fingerprint=s.state_fingerprint) for s in states)
        common = {
            "operation_id": self.operation_id, "lease_token": self.lease,
            "episode_id": UUID(self.episode_id), "group_id": GROUP, "mapping_revision": 0,
            "desired_support_digest": "a" * 64, **(phase_override or {}),
        }
        shell = GraphWriteReceipt(phase="shell_write_intent", episode_state_fingerprint=episode_fp, **common)
        entity_ids = (self.entity_id,) if self.entity_id else ()
        bulk = GraphWriteReceipt(
            phase="bulk_write_intent", episode_state_fingerprint=episode_fp,
            prior_episode_state_fingerprint=episode_fp, entity_ids=entity_ids,
            mention_ids=(self.mention_id,),
            fact_ids=tuple(sorted(s.fact_id for s in states)),
            existing_fact_states=tuple(sorted(states, key=lambda s: s.fact_id)),
            intended_fact_support=tuple(sorted(support, key=lambda s: s.fact_id)),
            prior_entity_state_fingerprints=tuple((e, False, None) for e in entity_ids),
            intended_entity_state_fingerprints=tuple((e, "b" * 64) for e in entity_ids),
            **common,
        )
        return (shell, bulk)

    def context(self, receipts: tuple[GraphWriteReceipt, ...] | None = None) -> OperationAuthorization:
        """Build the owner closure with a journaling callback that logs ordering."""
        graph = self.graph
        journal = self.journal

        @contextlib.asynccontextmanager
        async def fence() -> AsyncIterator[None]:
            graph.events.append(("fence_enter",))
            try:
                yield
            finally:
                graph.events.append(("fence_exit",))

        async def record(receipt: GraphWriteReceipt) -> None:
            graph.events.append(("intent", receipt.phase))
            if self.intent_error is not None:
                raise self.intent_error
            journal.append(receipt)

        return OperationAuthorization(
            group_id=GROUP, source_id=uuid4(), source_generation=1, operation_id=self.operation_id,
            lease_token=self.lease, evidence=(), fence=fence, authorize=AsyncMock(),
            canonicalize=AsyncMock(), validate_partition=self.validate_partition,
            record_write_intent=record, reasoning=SimpleNamespace(), small_reasoning=SimpleNamespace(),
            embedding=SimpleNamespace(dimensions=DIMS),
            partition_episode_ids=(self.episode_id, self.other_episode),
            partition_inventory_complete=True,
            receipt_history=receipts if receipts is not None else self.receipts(),
            node_recovery_actions=self.node_actions,
            authorize_node_recovery=self.authorize_node if self.node_actions else None,
        )

    async def delete(self, context: OperationAuthorization | None = None, **flags: bool) -> Any:
        """Run delete_episode with owned/tombstoned flags defaulting to True."""
        return await self.adapter.delete_episode(
            UUID(self.episode_id), context or self.context(),
            flags.get("known_owned", True), flags.get("tombstone_recorded", True),
        )


@pytest.fixture
def scenario(monkeypatch: pytest.MonkeyPatch) -> Scenario:
    """Fresh fake graph with one shared and one solely supported fact."""
    return Scenario(monkeypatch)


@pytest.mark.asyncio
@pytest.mark.parametrize("flags", [{"known_owned": False}, {"tombstone_recorded": False}])
async def test_requires_owned_tombstone(scenario: Scenario, flags: dict[str, bool]) -> None:
    """Unowned or untombstoned deletes raise before touching the fence or graph."""
    from modules.knowledge.temporal.adapter import GraphOperationError

    with pytest.raises(GraphOperationError, match="graph_delete_requires_owned_tombstone"):
        await scenario.delete(**flags)
    assert scenario.graph.events == []


@pytest.mark.asyncio
async def test_requires_episode_in_partition(scenario: Scenario) -> None:
    """An episode outside the authorized partition inventory is refused."""
    from modules.knowledge.temporal.adapter import GraphOperationError

    context = scenario.context()
    with pytest.raises(GraphOperationError, match="graph_delete_requires_owned_tombstone"):
        await scenario.adapter.delete_episode(uuid4(), context, True, True)


@pytest.mark.asyncio
async def test_not_ready_returns_unknown_without_fence(scenario: Scenario) -> None:
    """A graph that is not READY reports unknown and never enters the fence."""
    scenario.adapter.state = GraphState.UNAVAILABLE
    outcome = await scenario.delete()
    assert (outcome.removed, outcome.outcome, outcome.affected_edge_ids) == (False, "unknown", ())
    assert scenario.graph.events == []


@pytest.mark.asyncio
async def test_missing_receipts_unknown(scenario: Scenario) -> None:
    """Without any episode receipt the delete reports unknown and mutates nothing."""
    outcome = await scenario.delete(scenario.context(receipts=()))
    assert (outcome.removed, outcome.outcome) == (False, "unknown")
    assert [e for e in scenario.graph.events if e[0] in {"write", "delete_fact", "delete_episode"}] == []


@pytest.mark.asyncio
async def test_receipt_lease_mismatch_unknown(scenario: Scenario) -> None:
    """A receipt from another lease token fails closed as unknown."""
    other = scenario.receipts(phase_override={"lease_token": uuid4()})
    context = scenario.context(receipts=scenario.receipts())
    object.__setattr__(context, "receipt_history", other)
    outcome = await scenario.delete(context)
    assert (outcome.removed, outcome.outcome) == (False, "unknown")
    assert "intent" not in {e[0] for e in scenario.graph.events}


@pytest.mark.asyncio
async def test_receipt_phase_order_must_be_monotonic(scenario: Scenario) -> None:
    """Receipts out of phase order are rejected as unknown."""
    shell, bulk = scenario.receipts()
    outcome = await scenario.delete(scenario.context(receipts=(bulk, shell)))
    assert (outcome.removed, outcome.outcome) == (False, "unknown")
    assert "intent" not in {e[0] for e in scenario.graph.events}


@pytest.mark.asyncio
async def test_shared_and_removed_support_subtraction_orders_journal_first(scenario: Scenario) -> None:
    """Intent is journaled before any mutation; shared fact keeps other support, solo fact is deleted."""
    outcome = await scenario.delete()
    events = scenario.graph.events
    assert (outcome.removed, outcome.outcome) == (True, "unknown")
    assert set(outcome.affected_edge_ids) == {scenario.fact_shared, scenario.fact_solo}
    assert outcome.shared_edge_ids == (scenario.fact_shared,)
    assert outcome.stale_edge_ids == () and outcome.node_recovery_required_ids == ()
    assert events[0] == ("fence_enter",) and events[-1] == ("fence_exit",)
    first_mutation = min(i for i, e in enumerate(events) if e[0] in {"write", "delete_fact", "delete_episode"})
    assert events.index(("intent", "cleanup_write_intent")) < first_mutation
    assert len(scenario.journal) == 1 and scenario.journal[0].phase == "cleanup_write_intent"
    shared_flow = [e for e in events[first_mutation - 1:] if e in {
        ("read_edges", (scenario.fact_shared,)), ("write", scenario.fact_shared)}]
    assert shared_flow == [("read_edges", (scenario.fact_shared,)), ("write", scenario.fact_shared),
                           ("read_edges", (scenario.fact_shared,))]
    assert scenario.graph.edges[scenario.fact_shared].episodes == [scenario.other_episode]
    assert scenario.fact_solo not in scenario.graph.edges
    assert scenario.episode_id not in scenario.graph.episodes
    intended = {item.fact_id: item for item in scenario.journal[0].intended_fact_support}
    assert intended[scenario.fact_solo].episode_ids == ()
    assert intended[scenario.fact_shared].episode_ids == (scenario.other_episode,)


@pytest.mark.asyncio
async def test_solo_support_only_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no shared facts and no pending nodes the delete succeeds."""
    scenario = Scenario(monkeypatch, with_shared=False)
    outcome = await scenario.delete()
    assert (outcome.removed, outcome.outcome) == (True, "succeeded")
    assert outcome.shared_edge_ids == ()


@pytest.mark.asyncio
async def test_already_absent_episode_handles_node_not_found(scenario: Scenario) -> None:
    """NodeNotFoundError for the episode is treated as already absent (retry path)."""
    context = scenario.context()
    scenario.graph.episodes.pop(scenario.episode_id)
    scenario.graph.mentions.clear()
    outcome = await scenario.delete(context)
    # The episode shell is gone, so nothing is deleted again and the support is still subtracted.
    assert outcome.removed is True
    assert "delete_episode" not in {e[0] for e in scenario.graph.events}
    assert outcome.shared_edge_ids == (scenario.fact_shared,)


@pytest.mark.asyncio
async def test_intent_recording_failure_returns_partial_unknown(scenario: Scenario) -> None:
    """If the journal write fails no mutation happens and ids gathered so far are reported."""
    scenario.intent_error = RuntimeError("db down")
    outcome = await scenario.delete()
    assert (outcome.removed, outcome.outcome) == (False, "unknown")
    assert set(outcome.affected_edge_ids) == {scenario.fact_shared, scenario.fact_solo}
    assert outcome.shared_edge_ids == (scenario.fact_shared,)
    assert [e for e in scenario.graph.events if e[0] in {"write", "delete_fact", "delete_episode"}] == []


@pytest.mark.asyncio
async def test_unconfirmed_support_write_returns_partial_outcome(scenario: Scenario) -> None:
    """A rejected support write aborts remaining mutations but keeps affected/shared ids."""
    scenario.graph.fail_set_write = True
    outcome = await scenario.delete()
    assert (outcome.removed, outcome.outcome) == (False, "unknown")
    assert outcome.shared_edge_ids == (scenario.fact_shared,)
    assert set(outcome.affected_edge_ids) == {scenario.fact_shared, scenario.fact_solo}
    assert scenario.fact_solo in scenario.graph.edges
    assert scenario.episode_id in scenario.graph.episodes


@pytest.mark.asyncio
async def test_fact_changed_after_receipt_is_refused(scenario: Scenario) -> None:
    """A fact whose fingerprint drifted from every journaled state aborts before the journal."""
    context = scenario.context()
    scenario.graph.edges[scenario.fact_shared].fact = "changed"
    outcome = await scenario.delete(context)
    assert (outcome.removed, outcome.outcome) == (False, "unknown")
    assert "intent" not in {e[0] for e in scenario.graph.events}


@pytest.mark.asyncio
async def test_stale_fact_without_episode_support_is_reported_pending(scenario: Scenario) -> None:
    """A fact lacking this episode but invalidated at extraction time stays stale-pending."""
    edge = scenario.graph.edges[scenario.fact_shared]
    edge.episodes = [scenario.other_episode]
    invalidated = datetime(2026, 2, 1, tzinfo=UTC)
    edge.invalid_at = invalidated
    shell, bulk = scenario.receipts()
    prior = _fact_state(make_edge(scenario.fact_shared, scenario.source_node, scenario.target_node,
                              [scenario.other_episode]), DIMS)
    now_state = _fact_state(edge, DIMS)
    from dataclasses import replace
    existing = tuple(prior if s.fact_id == scenario.fact_shared else s for s in bulk.existing_fact_states)
    intended = tuple(
        ExactFactSupport(
            fact_id=scenario.fact_shared, episode_ids=now_state.episode_ids,
            source_node_id=now_state.source_node_id, target_node_id=now_state.target_node_id,
            invalid_at=invalidated, state_fingerprint=now_state.state_fingerprint,
        ) if s.fact_id == scenario.fact_shared else s for s in bulk.intended_fact_support)
    stale_bulk = replace(bulk, existing_fact_states=existing, intended_fact_support=intended)
    outcome = await scenario.delete(scenario.context(receipts=(shell, stale_bulk)))
    assert scenario.fact_shared in outcome.shared_edge_ids
    assert scenario.fact_shared in outcome.stale_edge_ids
    assert outcome.outcome == "unknown"


@pytest.mark.asyncio
async def test_node_recovery_converges_when_retained_node_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    """A retain action whose live node and incident links match clears the pending node id."""
    scenario = Scenario(monkeypatch, with_entity=True)
    entity = scenario.graph.entities[scenario.entity_id]
    scenario.node_actions = (CanonicalNodeRecoveryAction(
        graph_entity_uuid=scenario.entity_id,
        expected_current_state_fingerprint=_entity_state_fingerprint(entity, DIMS),
        action="retain",
    ),)
    outcome = await scenario.delete()
    assert outcome.node_recovery_required_ids == ()
    assert scenario.authorize_node.await_count == 2
    assert outcome.removed is True


@pytest.mark.asyncio
async def test_unretained_entity_ids_are_reported_for_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    """Journaled entities without a converged action are returned as recovery-required ids."""
    scenario = Scenario(monkeypatch, with_entity=True)
    outcome = await scenario.delete()
    assert outcome.node_recovery_required_ids == (scenario.entity_id,)
    assert outcome.outcome == "unknown"


@pytest.mark.asyncio
async def test_failure_after_intent_keeps_recovery_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    """The partial outcome from a mid-flight failure includes the journaled recovery ids."""
    scenario = Scenario(monkeypatch, with_entity=True)
    scenario.graph.fail_set_write = True
    outcome = await scenario.delete()
    assert outcome.removed is False
    assert outcome.node_recovery_required_ids == (scenario.entity_id,)
    assert outcome.stale_edge_ids == ()


@pytest.mark.asyncio
async def test_episode_state_drift_returns_unknown(scenario: Scenario) -> None:
    """A changed episode shell is refused before any journal or mutation."""
    context = scenario.context()
    scenario.graph.episodes[scenario.episode_id].content = "tampered"
    outcome = await scenario.delete(context)
    assert (outcome.removed, outcome.outcome) == (False, "unknown")
    assert "intent" not in {e[0] for e in scenario.graph.events}


@pytest.mark.asyncio
async def test_episode_in_other_partition_is_refused(scenario: Scenario) -> None:
    """An episode stored under another group id is refused with an unknown outcome."""
    scenario.graph.episodes[scenario.episode_id].group_id = "other"
    outcome = await scenario.delete()
    assert (outcome.removed, outcome.outcome) == (False, "unknown")
    assert [e for e in scenario.graph.events if e[0] in {"write", "delete_fact", "delete_episode"}] == []
