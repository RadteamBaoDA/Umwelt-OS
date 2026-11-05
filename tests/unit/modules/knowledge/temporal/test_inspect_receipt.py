"""Characterization tests for TemporalGraph.inspect_write_receipt and _record_bulk_write_intent."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from modules.knowledge.temporal.adapter import (
    GraphOperationError,
    GraphState,
    _entity_state_fingerprint,
    _episode_state_fingerprint,
    _fact_state,
)

from .fake_graph import DIMS, GROUP, FakeDriver
from .test_delete_episode import Scenario


def _scenario(monkeypatch: pytest.MonkeyPatch) -> Scenario:
    """Scenario with an entity whose mention edge targets that entity."""
    scenario = Scenario(monkeypatch, with_entity=True)
    scenario.graph.mentions[scenario.mention_id].target_node_uuid = scenario.entity_id
    return scenario


@pytest.mark.asyncio
async def test_inspect_reports_exact_ids_and_fingerprints(monkeypatch: pytest.MonkeyPatch) -> None:
    """A normal bulk receipt yields the exact present ids and current fingerprints."""
    scenario = _scenario(monkeypatch)
    receipt = scenario.receipts()[1]
    result = await scenario.adapter.inspect_write_receipt(receipt, scenario.context())
    graph = scenario.graph
    assert result.episode_present
    assert result.episode_state_fingerprint == _episode_state_fingerprint(
        graph.episodes[scenario.episode_id]
    )
    assert result.entity_ids_present == (scenario.entity_id,)
    assert result.current_entity_state_fingerprints == (
        (scenario.entity_id, _entity_state_fingerprint(graph.entities[scenario.entity_id], DIMS)),
    )
    assert result.mention_ids_present == (scenario.mention_id,)
    assert result.current_mention_links[0].relationship_type == "MENTIONS"
    assert set(result.fact_ids_present) == {scenario.fact_shared, scenario.fact_solo}
    assert result.current_fact_states == tuple(
        sorted(
            (_fact_state(graph.edges[f], DIMS) for f in result.fact_ids_present),
            key=lambda s: s.fact_id,
        )
    )
    assert result.incident_links == ()
    assert graph.events[0] == ("fence_enter",) and graph.events[-1] == ("fence_exit",)


@pytest.mark.asyncio
async def test_inspect_reports_incident_links(monkeypatch: pytest.MonkeyPatch) -> None:
    """Live incident links of journaled nodes are returned in inventory form."""
    scenario = _scenario(monkeypatch)
    edge_id = str(uuid4())
    scenario.graph.node_links[scenario.entity_id] = [
        {
            "edge_id": edge_id,
            "relationship_type": "REL",
            "source_node_id": scenario.source_node,
            "target_node_id": scenario.entity_id,
            "episode_ids": [scenario.other_episode],
        }
    ]
    result = await scenario.adapter.inspect_write_receipt(
        scenario.receipts()[1], scenario.context()
    )
    assert [
        (item.edge_id, item.relationship_type, item.episode_ids) for item in result.incident_links
    ] == [(edge_id, "REL", (scenario.other_episode,))]


@pytest.mark.asyncio
async def test_inspect_absent_episode(monkeypatch: pytest.MonkeyPatch) -> None:
    """A deleted episode is reported absent with no fingerprint."""
    scenario = _scenario(monkeypatch)
    receipt = scenario.receipts()[1]
    context = scenario.context()
    scenario.graph.episodes.clear()
    scenario.graph.mentions.clear()
    result = await scenario.adapter.inspect_write_receipt(receipt, context)
    assert not result.episode_present and result.episode_state_fingerprint is None
    assert result.mention_ids_present == ()


@pytest.mark.asyncio
async def test_inspect_rejects_foreign_lease(monkeypatch: pytest.MonkeyPatch) -> None:
    """A receipt from another lease is outside the authorized partition."""
    scenario = _scenario(monkeypatch)
    receipt = scenario.receipts(phase_override={"lease_token": uuid4()})[1]
    with pytest.raises(GraphOperationError, match="graph_receipt_outside_authorized_partition"):
        await scenario.adapter.inspect_write_receipt(receipt, scenario.context())
    assert scenario.graph.events == []


@pytest.mark.asyncio
async def test_inspect_requires_ready_graph(monkeypatch: pytest.MonkeyPatch) -> None:
    """After validation, a non-READY graph raises graph_unavailable."""
    scenario = _scenario(monkeypatch)
    scenario.adapter.state = GraphState.UNAVAILABLE
    with pytest.raises(GraphOperationError, match="graph_unavailable"):
        await scenario.adapter.inspect_write_receipt(scenario.receipts()[1], scenario.context())


@pytest.mark.asyncio
async def test_inspect_rejects_support_outside_partition(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fact whose support leaves the partition fails the partition-mismatch check."""
    scenario = _scenario(monkeypatch)
    receipt = scenario.receipts()[1]
    context = scenario.context()
    scenario.graph.edges[scenario.fact_solo].episodes = [str(uuid4())]
    with pytest.raises(GraphOperationError, match="graph_receipt_partition_mismatch"):
        await scenario.adapter.inspect_write_receipt(receipt, context)


@pytest.mark.asyncio
async def test_inspect_maps_unexpected_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dependency failures inside the window are re-mapped to a sanitized graph error."""
    scenario = _scenario(monkeypatch)
    scenario.validate_partition.side_effect = RuntimeError("secret detail")
    context = scenario.context()
    scenario.adapter._validate_partition = scenario.validate_partition  # type: ignore[method-assign]
    with pytest.raises(GraphOperationError) as caught:
        await scenario.adapter.inspect_write_receipt(scenario.receipts()[1], context)
    assert "secret" not in str(caught.value)


def _request(scenario: Scenario) -> Any:
    """Build the minimal episode request for bulk-intent journaling."""
    return SimpleNamespace(
        episode_id=UUID(scenario.episode_id),
        group_id=GROUP,
        mapping_revision=0,
        canonical_bindings=(),
    )


def _bulk_inputs(scenario: Scenario) -> dict[str, Any]:
    """Objects the writer would hand to the bulk intent for the fake graph's single episode."""
    graph = scenario.graph
    return {
        "episodes": [SimpleNamespace(**vars(graph.episodes[scenario.episode_id]))],
        "mentions": [graph.mentions[scenario.mention_id]],
        "nodes": [graph.entities[scenario.entity_id]],
        "facts": [graph.edges[scenario.fact_solo]],
    }


@pytest.mark.asyncio
async def test_bulk_intent_ids_phase_then_hydrated_phase(monkeypatch: pytest.MonkeyPatch) -> None:
    """IDs are journaled first (bulk_ids_intent); the hydrated pass journals final node state."""
    scenario = _scenario(monkeypatch)
    request = _request(scenario)
    inputs = _bulk_inputs(scenario)
    shell = scenario.receipts()[0]
    latest: list[Any] = [shell]
    context = scenario.context()
    driver = FakeDriver(scenario.graph)
    await scenario.adapter._record_bulk_write_intent(
        request, context, shell, latest, **inputs, driver=driver
    )
    assert latest[0].phase == "bulk_ids_intent" and scenario.journal[-1] is latest[0]
    assert latest[0].entity_ids == (scenario.entity_id,)
    assert latest[0].prior_entity_state_fingerprints[0][1] is True
    assert scenario.validate_partition.await_count == 1
    entity = scenario.graph.entities[scenario.entity_id]
    payload = {
        "uuid": scenario.entity_id,
        "group_id": GROUP,
        "name": entity.name,
        "summary": entity.summary,
        "created_at": entity.created_at,
        "labels": ["Entity"],
        "name_embedding": list(entity.name_embedding),
    }
    await scenario.adapter._record_bulk_write_intent(
        request, context, shell, latest, **inputs, driver=driver, final_node_payloads=[payload]
    )
    assert latest[0].phase == "bulk_write_intent"
    assert latest[0].intended_entity_state_fingerprints[0][0] == scenario.entity_id
    # ids pass + hydrated pass + recheck after the durable hydrated intent
    assert scenario.validate_partition.await_count == 3


@pytest.mark.asyncio
async def test_bulk_intent_rejects_foreign_group_and_wraps_journal_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Foreign-group objects and journal failures surface as stable graph errors."""
    scenario = _scenario(monkeypatch)
    request = _request(scenario)
    shell = scenario.receipts()[0]
    inputs = _bulk_inputs(scenario)
    inputs["nodes"] = [SimpleNamespace(**{**vars(inputs["nodes"][0]), "group_id": "other"})]
    with pytest.raises(GraphOperationError, match="graph_write_intent_identifiers_invalid"):
        await scenario.adapter._record_bulk_write_intent(
            request, scenario.context(), shell, [shell], **inputs, driver=FakeDriver(scenario.graph)
        )
    scenario.validate_partition.side_effect = RuntimeError("down")
    with pytest.raises(GraphOperationError, match="graph_write_intent_unavailable"):
        await scenario.adapter._record_bulk_write_intent(
            request,
            scenario.context(),
            shell,
            [shell],
            **_bulk_inputs(scenario),
            driver=FakeDriver(scenario.graph),
        )
