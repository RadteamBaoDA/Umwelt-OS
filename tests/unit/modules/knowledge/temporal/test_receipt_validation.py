"""Characterization tests for GraphWriteReceipt.__post_init__ validation (message per rule family)."""

from __future__ import annotations

import hashlib
from typing import Any
from uuid import uuid4

import pytest

from modules.knowledge.temporal.adapter import (
    ExactFactState,
    ExactFactSupport,
    ExactGraphLink,
    GraphWriteReceipt,
)

SHA = "a" * 64
OTHER = "b" * 64
ABSENT = hashlib.sha256(b"absent").hexdigest()
IDS = [str(uuid4()) for _ in range(6)]


def _base(**overrides: Any) -> dict[str, Any]:
    """Return valid shell-receipt keyword arguments, overridden per case."""
    values: dict[str, Any] = {
        "operation_id": uuid4(), "lease_token": uuid4(), "episode_id": uuid4(), "group_id": "g1",
        "mapping_revision": 0, "desired_support_digest": SHA, "phase": "shell_write_intent",
        "episode_state_fingerprint": SHA,
    }
    values.update(overrides)
    return values


def _fact(fact_id: str, *, existed: bool = True, fingerprint: str = SHA) -> tuple[ExactFactState, ExactFactSupport]:
    """Return matching prior and intended fact records for one fact id."""
    state = ExactFactState(fact_id, IDS[4], IDS[5], (), fingerprint, existed)
    support = ExactFactSupport(fact_id=fact_id, episode_ids=(), source_node_id=IDS[4], target_node_id=IDS[5],
                               state_fingerprint=fingerprint)
    return state, support


def _bulk(**overrides: Any) -> dict[str, Any]:
    """Return valid bulk_write_intent arguments with one node and one fact."""
    state, support = _fact(IDS[1])
    return _base(
        phase="bulk_write_intent", prior_episode_state_fingerprint=OTHER, entity_ids=(IDS[0],),
        fact_ids=(IDS[1],), existing_fact_states=(state,), intended_fact_support=(support,),
        prior_entity_state_fingerprints=((IDS[0], False, None),),
        intended_entity_state_fingerprints=((IDS[0], SHA),), **overrides,
    )


@pytest.mark.parametrize("values", [
    _base(),
    _bulk(),
    _base(phase="bulk_ids_intent", entity_ids=(IDS[0],),
          prior_entity_state_fingerprints=((IDS[0], True, SHA),)),
    _base(phase="canonical_node_write_intent", entity_ids=(IDS[0],),
          prior_entity_state_fingerprints=((IDS[0], False, None),),
          intended_entity_state_fingerprints=((IDS[0], SHA),)),
    _base(phase="cleanup_write_intent", prior_episode_state_fingerprint=OTHER, episode_state_fingerprint=None),
    _base(phase="cleanup_write_intent", episode_state_fingerprint=None, entity_ids=(IDS[0],),
          prior_entity_state_fingerprints=((IDS[0], False, None),),
          intended_entity_state_fingerprints=((IDS[0], None),), rebuild_absence_observed="node"),
])
def test_valid_receipts_construct(values: dict[str, Any]) -> None:
    """Every phase accepts its minimal consistent shape."""
    assert GraphWriteReceipt(**values).group_id == "g1"


_BOUNDS = "Graph receipt identifiers exceed their bounds"
_CASES: list[tuple[str, dict[str, Any]]] = [
    (_BOUNDS, _base(group_id="")),
    (_BOUNDS, _base(group_id="g" * 201)),
    (_BOUNDS, _base(mapping_revision=-1)),
    (_BOUNDS, _base(operation_id="not-a-uuid")),
    (_BOUNDS, _base(desired_support_digest="xyz")),
    (_BOUNDS, _base(entity_ids=("nope",))),
    (_BOUNDS, _base(entity_ids=(IDS[0], IDS[0]))),
    (_BOUNDS, _base(entity_ids=tuple(str(uuid4()) for _ in range(101)))),
    (_BOUNDS, _base(incident_links=tuple(
        ExactGraphLink(edge_id, "REL", IDS[4], IDS[5], ()) for edge_id in sorted(IDS[:2], reverse=True)))),
    (_BOUNDS, _base(prior_entity_state_fingerprints=((IDS[0], True, None),))),
    (_BOUNDS, _base(intended_entity_state_fingerprints=((IDS[0], "bad"),))),
    (_BOUNDS, _base(existing_fact_states=(ExactFactState(IDS[1], IDS[4], IDS[5], (), "bad", True),))),
    (_BOUNDS, _base(intended_fact_support=(
        ExactFactSupport(fact_id=IDS[1], episode_ids=("nope",), source_node_id=IDS[4], target_node_id=IDS[5],
                         state_fingerprint=SHA),))),
    ("Candidate receipt field support exceeds its bounds",
     _base(entity_ids=(IDS[0],), candidate_field_support=((IDS[0], "name", "bad", (IDS[2],)),))),
    ("Candidate receipt proof must identify a journaled node",
     _base(candidate_field_support=((IDS[0], "name", SHA, (IDS[2],)),))),
    ("Graph receipt fact IDs require complete prior and intended state",
     _base(phase="bulk_ids_intent", fact_ids=(IDS[1],))),
    ("Bulk ID receipt requires exact prior node states without asserting hydration",
     _base(phase="bulk_ids_intent", entity_ids=(IDS[0],))),
    ("Shell receipt requires its exact intended episode fingerprint", _base(episode_state_fingerprint=None)),
    ("Canonical node receipt requires seeded exact entity IDs", _base(phase="canonical_node_write_intent")),
    ("Bulk receipt requires exact prior and intended node and episode states",
     _bulk(episode_state_fingerprint=None)),
    ("Rebuild absence receipt must describe actual current exact absence",
     _base(rebuild_absence_observed="node")),
    ("Rebuild absence receipt must describe actual current exact absence",
     _base(phase="cleanup_write_intent", prior_episode_state_fingerprint=OTHER, rebuild_absence_observed="node")),
    ("Cleanup receipt requires prior episode or complete node states",
     _base(phase="cleanup_write_intent", episode_state_fingerprint=None)),
    ("Episode state fingerprints must be SHA256 digests", _base(episode_state_fingerprint="ZZ")),
    ("Episode state fingerprints must be SHA256 digests",
     _base(phase="cleanup_write_intent", episode_state_fingerprint=None, prior_episode_state_fingerprint="ZZ")),
]


@pytest.mark.parametrize(("message", "values"), _CASES)
def test_invalid_receipts_raise_family_message(message: str, values: dict[str, Any]) -> None:
    """Each violated rule family raises ValueError with its stable message."""
    with pytest.raises(ValueError, match=message):
        GraphWriteReceipt(**values)


def test_fact_absence_receipt_requires_matching_endpoints_and_absent_state() -> None:
    """A fact-absence cleanup receipt must carry one absent state and one absent intended support."""
    state, support = _fact(IDS[1], existed=False, fingerprint=ABSENT)
    ok = _base(phase="cleanup_write_intent", episode_state_fingerprint=None, fact_ids=(IDS[1],),
               existing_fact_states=(state,), intended_fact_support=(support,), rebuild_absence_observed="fact")
    assert GraphWriteReceipt(**ok).rebuild_absence_observed == "fact"
    present, _ = _fact(IDS[1], existed=True, fingerprint=ABSENT)
    with pytest.raises(ValueError, match="Rebuild absence receipt"):
        GraphWriteReceipt(**{**ok, "existing_fact_states": (present,)})
    other_state = ExactFactState(IDS[1], IDS[5], IDS[4], (), ABSENT, False)
    with pytest.raises(ValueError, match="Rebuild absence receipt"):
        GraphWriteReceipt(**{**ok, "existing_fact_states": (other_state,)})
