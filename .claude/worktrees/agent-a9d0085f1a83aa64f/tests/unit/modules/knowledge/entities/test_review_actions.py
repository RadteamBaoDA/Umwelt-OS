"""Characterization of ``assign_review_candidate`` / ``resolve_relationship_review`` ordering and fences."""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

import modules.knowledge.documents.public as documents_public
import modules.knowledge.relationships.public as relationships_public
from core.errors import error_status
from modules.knowledge.entities import public as ent
from modules.knowledge.entities.schemas import (
    EntityRelationshipReviewRequest,
    EntityReviewAssignmentRequest,
)
from tests.unit.entity_patching import patch_entities

WS, VERSION, DOC, SRC = uuid4(), uuid4(), uuid4(), uuid4()
CHUNK = uuid4()
FP, KEY = "a" * 64, "b" * 64


def _digest(snapshot: dict[str, Any]) -> str:
    """Digest of fenced identity fields."""
    return hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _entity_snapshot(candidate_id: Any) -> dict[str, Any]:
    """Entity review item with a valid digest."""
    snap = {
        "kind": "entity",
        "candidate_id": str(candidate_id),
        "candidate_type": "person",
        "candidate_name": "Zed",
        "candidate_key": KEY,
        "match_fingerprint": FP,
        "chunk_ids": [str(CHUNK)],
        "confidence": 0.5,
    }
    return {
        **snap,
        "snapshot_digest": _digest(snap),
        "reason": "ambiguous_identity",
        "possible_entity_ids": [],
    }


def _relationship_snapshot(candidate_id: Any) -> dict[str, Any]:
    """Relationship review item with a valid digest."""
    snap = {
        "kind": "relationship",
        "candidate_id": str(candidate_id),
        "candidate_name": "KNOWS",
        "relationship_type": "KNOWS",
        "source_candidate_key": "s",
        "target_candidate_key": "t",
        "chunk_id": str(CHUNK),
        "confidence": 0.5,
    }
    return {
        **snap,
        "snapshot_digest": _digest(snap),
        "reason": "relationship_endpoint_requires_review",
        "possible_entity_ids": [],
    }


class _Env(SimpleNamespace):
    """Holder of the shared fakes for one scenario."""


def _env(monkeypatch: pytest.MonkeyPatch, snapshot: dict[str, Any], *, generation: int = 3) -> _Env:
    """Patch every collaborator; ``env.log`` records ordered external calls."""
    log: list[str] = []
    work = SimpleNamespace(
        id=uuid4(), document_version_id=VERSION, source_generation=1, status="succeeded"
    )
    result = SimpleNamespace(id=uuid4(), work_id=work.id, review_json=[snapshot])
    scalars = [
        result,
        work,
        work,
        result,
        None,
    ]  # hints, FOR UPDATE rows, then the existing-decision probe

    async def scalar(_statement: Any) -> Any:
        log.append("scalar")
        return scalars.pop(0)

    session = SimpleNamespace(
        scalar=scalar, add=lambda row: log.append(f"add:{row.decision}"), flush=AsyncMock()
    )

    async def admit(session: Any, **kwargs: Any) -> str:
        log.append(f"admit:lock={kwargs.get('lock')}")
        return "fence"

    async def locator(*_a: Any, **_k: Any) -> Any:
        log.append("locator")
        return DOC, SRC

    async def lock_source(*_a: Any, **_k: Any) -> Any:
        log.append("lock_source")
        return SimpleNamespace(generation=generation)

    evidence_ref = SimpleNamespace(
        document_id=DOC,
        document_version_id=VERSION,
        source_id=SRC,
        current_source_generation=generation,
        chunk_id=CHUNK,
        observed_at=None,
    )

    async def lock_evidence(*_a: Any, **_k: Any) -> Any:
        log.append("lock_evidence")
        return [evidence_ref]

    async def decisions(*_a: Any, **kwargs: Any) -> dict[str, Any]:
        log.append(f"decisions:update={kwargs.get('for_update', False)}")
        return {}

    target = uuid4()

    async def entity_refs(_s: Any, ids: list[Any], **kwargs: Any) -> list[Any]:
        log.append(f"entity_refs:write={kwargs.get('for_write')}")
        return [
            SimpleNamespace(requested_id=i, canonical_id=i, type="person", revision=4) for i in ids
        ]

    async def membership(*_a: Any, **_k: Any) -> Any:
        log.append("membership")
        return uuid4()

    async def audit(*_a: Any, **kwargs: Any) -> None:
        log.append(f"audit:{kwargs['operation']}")

    async def commit(*_a: Any, **_k: Any) -> None:
        log.append("commit")

    patch_entities(monkeypatch, "_admit", admit)
    patch_entities(monkeypatch, "_actor", lambda _scope: 7)
    patch_entities(monkeypatch, "get_document_correction_decisions", decisions)
    patch_entities(monkeypatch, "get_entity_refs", entity_refs)
    patch_entities(monkeypatch, "record_extraction_membership", membership)
    patch_entities(monkeypatch, "record_owner_action", audit)
    patch_entities(monkeypatch, "commit_with_replay", commit)
    patch_entities(monkeypatch, "make_graph_change", lambda **_k: "change")
    monkeypatch.setattr(ent.sources, "lock_source", lock_source)
    monkeypatch.setattr(documents_public, "review_version_locator", locator)
    monkeypatch.setattr(documents_public, "lock_review_version_evidence", lock_evidence)
    return _Env(
        log=log,
        session=session,
        work=work,
        result=result,
        target=target,
        entity_refs=entity_refs,
        generation=generation,
    )


async def test_assign_success_orders_locks_and_marks_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Assignment locks Source, evidence, target, rows, evidence again, decisions; then writes and commits once."""
    candidate_id = uuid4()
    snapshot = _entity_snapshot(candidate_id)
    env = _env(monkeypatch, snapshot)
    payload = EntityReviewAssignmentRequest(
        result_id=env.result.id,
        snapshot_digest=snapshot["snapshot_digest"],
        expected_source_generation=1,
        expected_owner_generation=3,
        target_entity_id=env.target,
        expected_target_revision=4,
        reason="why",
    )
    out = await ent.assign_review_candidate(
        env.session,
        candidate_id,
        payload,
        scope=SimpleNamespace(workspace_id=WS),
        multi_workspace_enabled=False,
        actor_id=7,
    )
    assert env.log == [
        "admit:lock=True",
        "scalar",
        "scalar",
        "locator",
        "lock_source",
        "lock_evidence",
        "decisions:update=False",
        "entity_refs:write=True",
        "scalar",
        "scalar",
        "lock_evidence",
        "decisions:update=True",
        "membership",
        "scalar",
        "add:assign",
        "audit:entity_review_assign",
        "commit",
    ]
    assert out.target_entity_id == env.target and out.revision == 4 and len(out.membership_ids) == 1
    state = env.result.review_json[0]["decision_state"]
    assert state["kind"] == "assigned" and state["target_entity_id"] == str(env.target)
    assert (
        "decision_state" not in snapshot
    )  # the stored JSON list was replaced, not mutated in place
    env.session.flush.assert_awaited_once()


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        ("actor", "Review actor does not match"),
        ("digest", "snapshot changed; reload the queue"),
        ("future", "Future document scope"),
        ("generation", "Owner evidence fence changed"),
        ("decided", "already resolved"),
        ("revision", "Target entity changed"),
    ],
)
async def test_assign_rejections(
    monkeypatch: pytest.MonkeyPatch, mutate: str, message: str
) -> None:
    """Each fence failure raises its stable error before any write or commit."""
    candidate_id = uuid4()
    snapshot = _entity_snapshot(candidate_id)
    if mutate == "decided":
        snapshot["decision_state"] = {"kind": "assigned"}
    env = _env(monkeypatch, snapshot)
    kwargs: dict[str, Any] = {
        "result_id": env.result.id,
        "snapshot_digest": snapshot["snapshot_digest"],
        "expected_source_generation": 1,
        "expected_owner_generation": 9 if mutate == "generation" else 3,
        "target_entity_id": env.target,
        "expected_target_revision": 5 if mutate == "revision" else 4,
        "reason": "why",
    }
    if mutate == "digest":
        kwargs["snapshot_digest"] = "c" * 64
    if mutate == "future":
        kwargs["future_document_id"] = uuid4()
    payload = EntityReviewAssignmentRequest(**kwargs)
    with pytest.raises(Exception, match=message) as failure:
        await ent.assign_review_candidate(
            env.session,
            candidate_id,
            payload,
            scope=SimpleNamespace(workspace_id=WS),
            multi_workspace_enabled=False,
            actor_id=8 if mutate == "actor" else 7,
        )
    if mutate == "actor":
        assert error_status(failure.value) == 403
    else:
        assert isinstance(failure.value, (ValueError, LookupError))
    assert "commit" not in env.log and not any(entry.startswith("add:") for entry in env.log)


async def test_assign_unknown_candidate_is_lookup_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A candidate id absent from the result is a LookupError."""
    snapshot = _entity_snapshot(uuid4())
    env = _env(monkeypatch, snapshot)
    payload = EntityReviewAssignmentRequest(
        result_id=env.result.id,
        snapshot_digest=snapshot["snapshot_digest"],
        expected_source_generation=1,
        expected_owner_generation=3,
        target_entity_id=env.target,
        expected_target_revision=4,
        reason="why",
    )
    with pytest.raises(LookupError, match="no longer available"):
        await ent.assign_review_candidate(
            env.session,
            uuid4(),
            payload,
            scope=SimpleNamespace(workspace_id=WS),
            multi_workspace_enabled=False,
            actor_id=7,
        )


async def test_resolve_relationship_success_publishes_after_row_locks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Relationship resolution re-reads endpoint memberships after the row locks and publishes once."""
    candidate_id = uuid4()
    snapshot = _relationship_snapshot(candidate_id)
    env = _env(monkeypatch, snapshot)
    one, two = uuid4(), uuid4()
    work_id = env.work.id

    def member(key: str, entity: Any) -> Any:
        return SimpleNamespace(
            id=uuid4(),
            entity_id=entity,
            extraction_identity=str(work_id),
            candidate_key=key,
            document_version_id=VERSION,
            chunk_id=CHUNK,
        )

    members = [member("s", one), member("t", two)]

    async def scalars(_statement: Any) -> Any:
        env.log.append("memberships")
        return SimpleNamespace(all=lambda: members)

    env.session.scalars = scalars

    async def membership_refs(_s: Any, ids: list[Any], **kwargs: Any) -> list[Any]:
        env.log.append(f"membership_refs:write={kwargs.get('for_write')}")
        return [
            SimpleNamespace(
                id=m.id, entity_id=m.entity_id, document_version_id=VERSION, chunk_id=CHUNK
            )
            for m in members
        ]

    published: list[Any] = []

    async def publish(*_a: Any, **kwargs: Any) -> Any:
        env.log.append("publish")
        published.append(kwargs)
        return uuid4()

    patch_entities(monkeypatch, "get_membership_refs", membership_refs)
    monkeypatch.setattr(relationships_public, "publish_extracted_relationship", publish)
    payload = EntityRelationshipReviewRequest(
        result_id=env.result.id,
        snapshot_digest=snapshot["snapshot_digest"],
        expected_source_generation=1,
        expected_owner_generation=3,
        reason="why",
    )
    out = await ent.resolve_relationship_review(
        env.session,
        candidate_id,
        payload,
        scope=SimpleNamespace(workspace_id=WS),
        multi_workspace_enabled=False,
        actor_id=7,
    )
    assert env.log == [
        "admit:lock=True",
        "scalar",
        "scalar",
        "locator",
        "lock_source",
        "lock_evidence",
        "memberships",
        "entity_refs:write=True",
        "membership_refs:write=True",
        "scalar",
        "scalar",
        "memberships",
        "publish",
        "audit:relationship_review_resolve",
        "commit",
    ]
    assert published[0]["relationship_type"] == "KNOWS" and published[0]["confidence"] == 0.5
    assert env.result.review_json[0]["decision_state"] == {
        "kind": "resolved",
        "relationship_id": str(out.relationship_id),
    }


async def test_resolve_rejects_non_relationship_and_bad_selector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-relationship snapshot is a LookupError; identical endpoint keys are a ValueError."""
    candidate_id = uuid4()
    entity_snapshot = _entity_snapshot(candidate_id)
    env = _env(monkeypatch, entity_snapshot)
    payload = EntityRelationshipReviewRequest(
        result_id=env.result.id,
        snapshot_digest=entity_snapshot["snapshot_digest"],
        expected_source_generation=1,
        expected_owner_generation=3,
        reason="why",
    )
    scope = SimpleNamespace(workspace_id=WS)
    with pytest.raises(LookupError, match="snapshot is unavailable"):
        await ent.resolve_relationship_review(
            env.session,
            candidate_id,
            payload,
            scope=scope,
            multi_workspace_enabled=False,
            actor_id=7,
        )
    same = _relationship_snapshot(candidate_id)
    same.update(target_candidate_key="s")
    same["snapshot_digest"] = _digest(
        {
            k: same[k]
            for k in (
                "kind",
                "candidate_id",
                "candidate_name",
                "relationship_type",
                "source_candidate_key",
                "target_candidate_key",
                "chunk_id",
                "confidence",
            )
        }
    )
    env = _env(monkeypatch, same)
    payload = EntityRelationshipReviewRequest(
        result_id=env.result.id,
        snapshot_digest=same["snapshot_digest"],
        expected_source_generation=1,
        expected_owner_generation=3,
        reason="why",
    )
    with pytest.raises(ValueError, match="endpoint selector is invalid"):
        await ent.resolve_relationship_review(
            env.session,
            candidate_id,
            payload,
            scope=scope,
            multi_workspace_enabled=False,
            actor_id=7,
        )
