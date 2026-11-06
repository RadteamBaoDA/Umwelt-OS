"""Characterization tests for the named steps of worker._recover and process_graph_operation."""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from modules.knowledge.temporal import worker
from modules.knowledge.temporal.adapter import (
    CanonicalNodeRecoveryAction,
    DispatchOwnership,
    ExactFactRecoveryAction,
    ExactFactState,
    ExactFactSupport,
    ExactGraphLink,
    GraphOperationError,
    GraphOperationUnknown,
    GraphReceiptInspection,
    GraphWriteReceipt,
)

SHA = "a" * 64
OTHER = "b" * 64


def _receipt(**values: Any) -> GraphWriteReceipt:
    """Build a receipt for a fixed episode with overrides."""
    base: dict[str, Any] = {
        "operation_id": uuid4(),
        "lease_token": uuid4(),
        "episode_id": uuid4(),
        "group_id": "g",
        "mapping_revision": 0,
        "desired_support_digest": SHA,
        "phase": "cleanup_write_intent",
        "prior_episode_state_fingerprint": SHA,
    }
    base.update(values)
    return GraphWriteReceipt(**base)


def test_initial_node_actions_replace_bound_and_delete_orphans(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bound nodes are replaced from current support; unbound journaled nodes are deleted as orphans."""
    bound, orphan = sorted([str(uuid4()), str(uuid4())])
    binding = SimpleNamespace(graph_entity_uuid=bound, node_name="n")
    created = datetime(2026, 1, 1, tzinfo=UTC)
    receipt = _receipt(
        entity_ids=(bound, orphan),
        prior_entity_state_fingerprints=((bound, False, None), (orphan, False, None)),
        intended_entity_state_fingerprints=((bound, SHA), (orphan, OTHER)),
        prior_episode_state_fingerprint=None,
    )
    context = SimpleNamespace(receipt_history=(receipt,), canonical_bindings=(binding,))
    operation = SimpleNamespace(replacement_created_at=created)
    created_with: list[tuple[Any, ...]] = []

    class Spy:
        """Record constructor arguments (the stand-in binding cannot satisfy real action validation)."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            created_with.append((args, kwargs))

    monkeypatch.setattr(worker, "CanonicalNodeRecoveryAction", Spy)
    result = worker._initial_node_actions(context, operation)  # type: ignore[arg-type]
    assert len(result) == 2
    by_id = {args[0]: (args, kwargs) for args, kwargs in created_with}
    assert by_id[bound][0][1:3] == (SHA, "replace_from_current_support")
    assert by_id[bound][1] == {"replacement_binding": binding, "replacement_created_at": created}
    assert by_id[orphan][0][1:3] == (OTHER, "delete_orphan")
    assert by_id[orphan][1] == {"replacement_binding": None, "replacement_created_at": None}


def test_bind_node_actions_pins_fingerprint_and_incident_links() -> None:
    """Each action receives the inspected fingerprint and only links touching its node."""
    node, other = str(uuid4()), str(uuid4())
    link_in = ExactGraphLink(str(uuid4()), "REL", other, node, ())
    link_out = ExactGraphLink(str(uuid4()), "REL", other, other, ())
    inspection = GraphReceiptInspection(
        episode_present=True,
        episode_state_fingerprint=SHA,
        entity_ids_present=(node,),
        current_entity_state_fingerprints=((node, OTHER),),
        incident_links=(link_in, link_out),
        mention_ids_present=(),
        current_mention_links=(),
        fact_ids_present=(),
        current_fact_states=(),
        current_fact_recovery_fingerprints=(),
    )
    action = CanonicalNodeRecoveryAction(node, SHA, "delete_orphan")
    (bound,) = worker._bind_node_actions((action,), inspection)
    assert bound.expected_current_state_fingerprint == OTHER
    assert bound.expected_incident_links == (link_in,)
    assert bound.action == "delete_orphan"


@pytest.mark.asyncio
async def test_prepare_node_actions_schedules_rebuild_for_shared_orphans(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An orphan with links supported by other episodes becomes delete_for_rebuild with mapping ids."""
    node, mine, theirs = str(uuid4()), str(uuid4()), str(uuid4())
    dependency = uuid4()
    mark = AsyncMock(return_value=(dependency,))
    monkeypatch.setattr(worker, "_mark_dependents_for_rebuild", mark)
    shared = ExactGraphLink(str(uuid4()), "REL", str(uuid4()), node, tuple(sorted([mine, theirs])))
    private = ExactGraphLink(str(uuid4()), "REL", str(uuid4()), str(uuid4()), (mine,))
    shared_action = CanonicalNodeRecoveryAction(
        node, SHA, "delete_orphan", expected_incident_links=(shared,)
    )
    own_node = str(uuid4())
    own_action = CanonicalNodeRecoveryAction(
        own_node, SHA, "delete_orphan", expected_incident_links=(private,)
    )
    mapping = SimpleNamespace(episode_id=mine)
    result = await worker._prepare_node_actions(
        MagicMock(), MagicMock(), MagicMock(), mapping, uuid4(), [], (shared_action, own_action)
    )  # type: ignore[arg-type]
    by_node = {item.graph_entity_uuid: item for item in result}
    assert by_node[node].action == "delete_for_rebuild" and by_node[node].rebuild_mapping_ids == (
        dependency,
    )
    assert by_node[own_node].action == "delete_orphan"
    mark.assert_awaited_once()
    assert mark.await_args.args[-1] == {("node", node), ("fact", shared.edge_id)}


def _fact_inputs() -> tuple[Any, ...]:
    """Return (fact ids, receipts, inspection) with one fact supported by this and a surviving episode."""
    fact, source, target = str(uuid4()), str(uuid4()), str(uuid4())
    mine, survivor = str(uuid4()), str(uuid4())
    state = ExactFactState(fact, source, target, tuple(sorted([mine, survivor])), SHA, True)
    support = ExactFactSupport(
        fact_id=fact,
        episode_ids=tuple(sorted([mine, survivor])),
        source_node_id=source,
        target_node_id=target,
        state_fingerprint=SHA,
    )
    receipt = _receipt(
        phase="bulk_write_intent",
        episode_state_fingerprint=OTHER,
        prior_episode_state_fingerprint=SHA,
        fact_ids=(fact,),
        existing_fact_states=(state,),
        intended_fact_support=(support,),
    )
    inspection = GraphReceiptInspection(
        episode_present=True,
        episode_state_fingerprint=OTHER,
        entity_ids_present=(),
        current_entity_state_fingerprints=(),
        incident_links=(),
        mention_ids_present=(),
        current_mention_links=(),
        fact_ids_present=(fact,),
        current_fact_states=(state,),
        current_fact_recovery_fingerprints=(),
    )
    return fact, mine, survivor, receipt, inspection, source, target


@pytest.mark.asyncio
async def test_build_fact_actions_fresh_replacement_or_rebuild_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Surviving support yields the fresh action; recognised proof errors fall back to a rebuild."""
    fact, mine, survivor, receipt, inspection, source, target = _fact_inputs()
    mapping = SimpleNamespace(episode_id=mine)
    fresh = SimpleNamespace(marker="fresh")
    monkeypatch.setattr(worker, "_fresh_fact_action", AsyncMock(return_value=fresh))
    args = (
        MagicMock(),
        MagicMock(),
        MagicMock(),
        {},
        MagicMock(),
        mapping,
        uuid4(),
        SimpleNamespace(authorize_rebuild_absence=AsyncMock()),
        [],
        (receipt,),
        inspection,
    )
    assert await worker._build_fact_actions(*args) == [fresh]  # type: ignore[arg-type]
    call = worker._fresh_fact_action.await_args  # type: ignore[attr-defined]
    assert call.args[-5:] == (fact, source, target, SHA, {survivor})
    dependency = uuid4()
    monkeypatch.setattr(
        worker,
        "_fresh_fact_action",
        AsyncMock(side_effect=GraphOperationError("graph_candidate_fact_requires_rebuild")),
    )
    monkeypatch.setattr(
        worker, "_mark_dependents_for_rebuild", AsyncMock(return_value=(dependency,))
    )
    (action,) = await worker._build_fact_actions(*args)  # type: ignore[arg-type]
    assert isinstance(action, ExactFactRecoveryAction)
    assert (action.action, action.rebuild_mapping_ids) == ("delete_for_rebuild", (dependency,))
    monkeypatch.setattr(
        worker, "_fresh_fact_action", AsyncMock(side_effect=GraphOperationError("graph_other"))
    )
    with pytest.raises(GraphOperationError, match="graph_other"):
        await worker._build_fact_actions(*args)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_build_fact_actions_deletes_unsupported_and_consumes_absence_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no surviving support the fact is deleted; an absent fact first consumes the rebuild-absence proof."""
    fact, mine, survivor, receipt, inspection, _source, _target = _fact_inputs()
    mapping = SimpleNamespace(episode_id=mine)
    absence = AsyncMock()
    context = SimpleNamespace(authorize_rebuild_absence=absence)
    missing = GraphReceiptInspection(
        **{**inspection.__dict__, "current_fact_states": (), "fact_ids_present": ()}
    )
    terminal = SimpleNamespace(
        episode_id=survivor,
        tombstoned=True,
        external_state="absent",
        applied_revision=1,
        desired_revision=1,
        applied_digest="d",
        desired_digest="d",
        error_code=None,
    )
    monkeypatch.setattr(worker, "_fresh_fact_action", AsyncMock(side_effect=AssertionError))
    (action,) = await worker._build_fact_actions(
        MagicMock(),
        MagicMock(),
        MagicMock(),
        {},
        MagicMock(),
        mapping,
        uuid4(),
        context,
        [terminal],  # type: ignore[arg-type]
        (receipt,),
        missing,
    )
    absence.assert_awaited_once_with("fact", fact)
    assert (action.action, action.expected_current_state_fingerprint) == ("delete_unsupported", SHA)


@pytest.mark.asyncio
async def test_verify_prior_dispatches_journals_cessation_or_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each unfinished prior dispatch must verify as ceased before its cessation is journaled."""
    operation = SimpleNamespace(id=uuid4(), cessation_verified_at=None, cessation_reason=None)
    mapping = SimpleNamespace(partition_id=uuid4())
    prior = SimpleNamespace(
        id=1, server_run_id="a" * 40, client_id=7, cessation_verified_at=None, cessation_reason=None
    )
    session = MagicMock()
    session.scalars = AsyncMock(return_value=SimpleNamespace(all=lambda: [prior]))
    rows = iter([operation, prior])

    @contextlib.asynccontextmanager
    async def stopped() -> Any:
        yield MagicMock()

    monkeypatch.setattr(worker, "_admit", AsyncMock())
    monkeypatch.setattr(worker, "_get", AsyncMock(side_effect=lambda *a, **k: next(rows)))
    commit = AsyncMock()
    monkeypatch.setattr(worker, "_commit", commit)
    graph = SimpleNamespace(verify_dispatch_cessation=AsyncMock(return_value=False))
    adm = SimpleNamespace(scope=SimpleNamespace(workspace_id=uuid4()))
    with pytest.raises(GraphOperationUnknown, match="graph_prior_dispatch_still_unknown"):
        await worker._verify_prior_dispatches(
            graph, stopped, adm, session, operation, mapping, MagicMock()
        )  # type: ignore[arg-type]
    previous = graph.verify_dispatch_cessation.await_args.args[0]
    assert previous == DispatchOwnership(operation.id, str(mapping.partition_id), "a" * 40, 7)
    commit.assert_not_awaited()
    graph.verify_dispatch_cessation.return_value = True
    await worker._verify_prior_dispatches(
        graph, stopped, adm, session, operation, mapping, MagicMock()
    )  # type: ignore[arg-type]
    assert operation.cessation_reason == prior.cessation_reason == "owned_sync_client_killed"
    assert operation.cessation_verified_at is not None and prior.cessation_verified_at is not None
    commit.assert_awaited_once()


def test_readback_matches_desired_requires_every_fingerprint() -> None:
    """The interrupted-upsert shortcut needs revision, digest, episode, nodes, facts and mentions to match."""
    _fact, mine, _survivor, receipt, inspection, _source, _target = _fact_inputs()
    mapping = SimpleNamespace(desired_revision=2, desired_digest="d", episode_id=mine)
    operation = SimpleNamespace(desired_revision=2, desired_digest="d")
    context = SimpleNamespace(canonical_bindings=())
    bulk = receipt
    match = worker._readback_matches_desired
    assert match(mapping, operation, context, (receipt,), inspection, bulk)  # type: ignore[arg-type]
    assert not match(
        SimpleNamespace(**{**vars(mapping), "desired_digest": "x"}),
        operation,
        context,  # type: ignore[arg-type]
        (receipt,),
        inspection,
        bulk,
    )
    drifted = GraphReceiptInspection(**{**inspection.__dict__, "episode_state_fingerprint": SHA})
    assert not match(mapping, operation, context, (receipt,), drifted, bulk)  # type: ignore[arg-type]
    missing_fact = GraphReceiptInspection(**{**inspection.__dict__, "current_fact_states": ()})
    assert not match(mapping, operation, context, (receipt,), missing_fact, bulk)  # type: ignore[arg-type]
