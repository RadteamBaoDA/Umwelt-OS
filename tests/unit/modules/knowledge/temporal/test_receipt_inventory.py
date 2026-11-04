"""Characterization tests for worker._receipt_inventory witness selection and ledger validation."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from pydantic import TypeAdapter

from modules.knowledge.temporal import worker
from modules.knowledge.temporal.adapter import (
    ExactFactState,
    ExactFactSupport,
    GraphOperationError,
    GraphReceiptInspection,
    GraphWriteReceipt,
)

OPERATION = uuid4()
LEASE = uuid4()
EPISODE = uuid4()
NODE = str(uuid4())
FACT = str(uuid4())
SOURCE = str(uuid4())
TARGET = str(uuid4())
OTHER_EPISODE = str(uuid4())
ADAPTER = TypeAdapter(GraphWriteReceipt)


def _fp(number: int) -> str:
    """Return a deterministic SHA256-shaped fingerprint."""
    return f"{number:064x}"


def _common(**values: Any) -> dict[str, Any]:
    """Receipt fields shared by the whole ledger."""
    base: dict[str, Any] = {
        "operation_id": OPERATION, "lease_token": LEASE, "episode_id": EPISODE, "group_id": "g",
        "mapping_revision": 0, "desired_support_digest": _fp(1),
    }
    base.update(values)
    return base


def _cleanup(step: int) -> GraphWriteReceipt:
    """Cleanup receipt moving node NODE from fingerprint ``step`` to ``step + 1``."""
    return GraphWriteReceipt(
        phase="cleanup_write_intent", prior_episode_state_fingerprint=_fp(900), entity_ids=(NODE,),
        prior_entity_state_fingerprints=((NODE, True, _fp(step)),),
        intended_entity_state_fingerprints=((NODE, _fp(step + 1)),), **_common())


def _ledger() -> list[GraphWriteReceipt]:
    """Shell, bulk and five cleanup receipts for the same node."""
    shell = GraphWriteReceipt(phase="shell_write_intent", episode_state_fingerprint=_fp(900), **_common())
    bulk = GraphWriteReceipt(
        phase="bulk_write_intent", episode_state_fingerprint=_fp(901), prior_episode_state_fingerprint=_fp(900),
        entity_ids=(NODE,), prior_entity_state_fingerprints=((NODE, False, None),),
        intended_entity_state_fingerprints=((NODE, _fp(2)),), **_common())
    return [shell, bulk, *(_cleanup(step) for step in range(2, 7))]


def _session(receipts: list[GraphWriteReceipt], page: int = 100) -> Any:
    """Session whose ``scalars`` pages the encoded ledger like the SQL ``limit`` would."""
    rows = [SimpleNamespace(sequence=index, payload=ADAPTER.dump_python(receipt, mode="json"))
            for index, receipt in enumerate(receipts)]
    calls: list[int] = []

    async def scalars(query: Any) -> Any:
        """Return the next page: rows at or after the requested sequence."""
        calls.append(len(calls))
        compiled = str(query.compile(compile_kwargs={"literal_binds": True}))
        start = int(compiled.split("sequence >= ")[1].split()[0])
        stop = int(compiled.split("sequence < ")[1].split()[0]) if "sequence < " in compiled else len(rows)
        return SimpleNamespace(all=lambda: rows[start:min(start + page, stop)])

    session = SimpleNamespace(scalars=AsyncMock(side_effect=scalars))
    session.calls = calls  # type: ignore[attr-defined]
    return session


def _adm() -> Any:
    """Admission stand-in exposing only the workspace id."""
    return SimpleNamespace(scope=SimpleNamespace(workspace_id=uuid4()))


def _inspection(**values: Any) -> GraphReceiptInspection:
    """Inspection with no live objects unless overridden."""
    base: dict[str, Any] = {
        "episode_present": True, "episode_state_fingerprint": None, "entity_ids_present": (),
        "current_entity_state_fingerprints": (), "incident_links": (), "mention_ids_present": (),
        "current_mention_links": (), "fact_ids_present": (), "current_fact_states": (),
        "current_fact_recovery_fingerprints": (),
    }
    base.update(values)
    return GraphReceiptInspection(**base)


@pytest.mark.asyncio
async def test_empty_ledger_has_no_witnesses() -> None:
    """No receipts yields no history and no aggregate."""
    assert await worker._receipt_inventory(_session([]), OPERATION, adm=_adm()) == ((), None)


@pytest.mark.asyncio
async def test_selects_first_latest_and_current_matching_witnesses() -> None:
    """Only first/latest per key and the receipt matching the live node state are retained."""
    ledger = _ledger()
    inspection = _inspection(current_entity_state_fingerprints=((NODE, _fp(4)),))
    receipts, aggregate = await worker._receipt_inventory(
        _session(ledger), OPERATION, adm=_adm(), inspection=inspection)
    assert aggregate is not None
    assert aggregate.witness_sequences == (0, 1, 2, 4, 6)
    assert receipts == tuple(ledger[index] for index in (0, 1, 2, 4, 6))
    assert aggregate.ledger_count == 7 and aggregate.entity_ids == (NODE,)
    assert (aggregate.operation_id, aggregate.lease_token, aggregate.group_id) == (OPERATION, LEASE, "g")
    assert aggregate.mention_ids == aggregate.fact_ids == aggregate.incident_link_ids == ()
    again = await worker._receipt_inventory(_session(ledger), OPERATION, adm=_adm(), inspection=inspection)
    assert again[1] is not None and again[1].ledger_digest == aggregate.ledger_digest
    changed = _ledger()
    changed[3] = _cleanup(30)
    other = await worker._receipt_inventory(_session(changed), OPERATION, adm=_adm(), inspection=inspection)
    assert other[1] is not None and other[1].ledger_digest != aggregate.ledger_digest


@pytest.mark.asyncio
async def test_without_inspection_only_first_and_latest_are_kept() -> None:
    """Without a live readback there is no current-matching witness."""
    _, aggregate = await worker._receipt_inventory(_session(_ledger()), OPERATION, adm=_adm())
    assert aggregate is not None and aggregate.witness_sequences == (0, 1, 2, 6)


@pytest.mark.asyncio
async def test_extra_witness_sequences_and_prefix_bound() -> None:
    """Requested extra sequences are included; unknown ones and a short prefix are rejected."""
    ledger = _ledger()
    _, aggregate = await worker._receipt_inventory(
        _session(ledger), OPERATION, 5, adm=_adm(), witness_sequences=(3,))
    assert aggregate is not None
    assert aggregate.ledger_count == 5 and aggregate.witness_sequences == (0, 1, 2, 3, 4)
    with pytest.raises(GraphOperationError, match="graph_recovery_witness_inventory_invalid"):
        await worker._receipt_inventory(_session(ledger), OPERATION, adm=_adm(), witness_sequences=(99,))
    with pytest.raises(GraphOperationError, match="graph_receipt_prefix_missing"):
        await worker._receipt_inventory(_session(ledger[:3]), OPERATION, 5, adm=_adm())


@pytest.mark.asyncio
async def test_sequence_gap_is_invalid() -> None:
    """A missing sequence number invalidates the whole inventory."""
    session = _session(_ledger())
    rows = (await session.scalars(SimpleNamespace(compile=lambda **_k: "sequence >= 0"))).all()
    gapped = [rows[0], SimpleNamespace(sequence=2, payload=rows[1].payload)]
    session.scalars = AsyncMock(return_value=SimpleNamespace(all=lambda: gapped))
    with pytest.raises(GraphOperationError, match="graph_receipt_inventory_invalid"):
        await worker._receipt_inventory(session, OPERATION, adm=_adm())


@pytest.mark.asyncio
async def test_pages_through_more_than_one_batch() -> None:
    """Ledgers longer than one page are read to the end."""
    session = _session(_ledger(), page=3)
    _, aggregate = await worker._receipt_inventory(session, OPERATION, adm=_adm())
    assert aggregate is not None and aggregate.ledger_count == 7 and len(session.calls) >= 3


@pytest.mark.asyncio
async def test_shared_and_stale_obligation_receipts_are_retained() -> None:
    """A cleanup receipt creating a shared-fact obligation stays a witness even mid-ledger."""
    mine = str(EPISODE)
    prior = ExactFactState(FACT, SOURCE, TARGET, tuple(sorted([mine, OTHER_EPISODE])), _fp(10), True)
    intended = ExactFactSupport(fact_id=FACT, episode_ids=(OTHER_EPISODE,), source_node_id=SOURCE,
                                target_node_id=TARGET, state_fingerprint=_fp(11))
    obligation = GraphWriteReceipt(
        phase="cleanup_write_intent", prior_episode_state_fingerprint=_fp(900), fact_ids=(FACT,),
        existing_fact_states=(prior,), intended_fact_support=(intended,), **_common())
    ledger = [_ledger()[0], obligation, *(_cleanup(step) for step in range(2, 5))]
    _, aggregate = await worker._receipt_inventory(_session(ledger), OPERATION, adm=_adm())
    assert aggregate is not None
    assert 1 in aggregate.witness_sequences and aggregate.fact_ids == (FACT,)
    # The fact fingerprint matching the live readback also keeps its receipt.
    live = _inspection(current_fact_states=(ExactFactState(
        FACT, SOURCE, TARGET, (OTHER_EPISODE,), _fp(10), True),))
    _, matched = await worker._receipt_inventory(_session(ledger), OPERATION, adm=_adm(), inspection=live)
    assert matched is not None and 1 in matched.witness_sequences


@pytest.mark.asyncio
async def test_effect_union_over_bound_is_rejected() -> None:
    """More than 100 distinct physical effects across the ledger abort the inventory."""
    ledger: list[GraphWriteReceipt] = []
    for _ in range(11):
        ids = tuple(sorted(str(uuid4()) for _ in range(10)))
        ledger.append(GraphWriteReceipt(
            phase="cleanup_write_intent", prior_episode_state_fingerprint=_fp(900), mention_ids=ids, **_common()))
    with pytest.raises(GraphOperationError, match="graph_recovery_effect_inventory_over_bound"):
        await worker._receipt_inventory(_session(ledger), OPERATION, adm=_adm())
    assert isinstance(UUID(NODE), UUID)
