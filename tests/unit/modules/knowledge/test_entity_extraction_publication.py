"""Characterization of entity extraction resolution and publication inside ``process_entity_extraction_work``.

These tests pin the observable outputs (review snapshots, facts, collaborator call order and arguments,
failure codes) of the post-inference phase so splitting the worker into helpers cannot change behaviour.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from modules.knowledge.entities import worker as entities_worker
from modules.knowledge.entities.extraction import ExtractedEntity, ExtractedRelationship
from modules.knowledge.entities.schemas import canonicalize_name
from tests.unit.modules.knowledge import test_extraction_lock_release as lock_tests
from tests.unit.modules.knowledge.test_extraction_lock_release import SRC, _harness

C1, C2, C3 = uuid4(), uuid4(), uuid4()


def _entity(
    key: str,
    name: str,
    chunks: list[UUID],
    *,
    type_: str = "person",
    description: str | None = None,
) -> ExtractedEntity:
    """Build a validated extracted entity candidate."""
    return ExtractedEntity.model_validate(
        {
            "key": key,
            "name": name,
            "type": type_,
            "description": description,
            "chunk_ids": chunks,
            "confidence": 0.9,
        }
    )


def _relation(
    source: str, target: str, chunk: UUID, type_: str = "WORKS_WITH"
) -> ExtractedRelationship:
    """Build a validated extracted relationship."""
    return ExtractedRelationship.model_validate(
        {
            "source_key": source,
            "target_key": target,
            "type": type_,
            "chunk_id": chunk,
            "confidence": 0.8,
        }
    )


def _output(
    entities: list[ExtractedEntity], relationships: list[ExtractedRelationship] | None = None
) -> Any:
    """Build an extraction output bypassing duplicate-key validation so worker checks can be exercised."""
    return SimpleNamespace(entities=entities, relationships=relationships or [])


class _Run(SimpleNamespace):
    """Holder for the patched collaborators of one run."""


def _setup(
    monkeypatch: pytest.MonkeyPatch,
    extracted: Any,
    *,
    known: dict[str, tuple[list[dict[str, Any]], bool]] | None = None,
    decisions: Any = None,
) -> _Run:
    """Patch the entity worker so ``extracted`` flows through resolution and publication with call logging."""
    m = _harness(monkeypatch, "entities")
    log: list[tuple[str, Any]] = []
    chunks = [SimpleNamespace(id=cid, content="text") for cid in (C1, C2, C3)]
    data = SimpleNamespace(
        document_id=m.ready.document_id,
        source_id=SRC,
        source_generation=1,
        chunks=chunks,
        observed_at=None,
    )
    monkeypatch.setattr(
        entities_worker.documents, "read_extraction_input", AsyncMock(return_value=data)
    )
    monkeypatch.setattr(
        entities_worker, "response_content", lambda _r: (extracted, "actual-model", {"tokens": 3})
    )
    refs = [SimpleNamespace(chunk_id=cid) for cid in (C1, C2, C3)]

    async def evidence(session: Any, **kwargs: Any) -> list[Any]:
        log.append(("evidence", sorted(kwargs["chunk_ids"], key=str)))
        return [ref for ref in refs if ref.chunk_id in kwargs["chunk_ids"]]

    async def candidates(session: Any, entity_type: str, names: list[str], **_k: Any) -> Any:
        log.append(("candidates", (entity_type, list(names))))
        return (known or {}).get(entity_type, ([], False))

    async def decide(session: Any, *_a: Any, **kwargs: Any) -> Any:
        log.append(("decisions", bool(kwargs.get("for_update"))))
        return decisions(_a) if callable(decisions) else (decisions or {})

    async def get_refs(session: Any, ids: list[UUID], **kwargs: Any) -> list[Any]:
        log.append(("refs", (list(ids), kwargs["for_write"])))
        return [
            SimpleNamespace(requested_id=i, type="person", revision=ref_revision(i)) for i in ids
        ]

    def ref_revision(identifier: UUID) -> int:
        for items, _ in (known or {}).values():
            for item in items:
                if item["id"] == identifier:
                    return int(item["revision"])
        return 1

    created: list[UUID] = []

    async def create(session: Any, entity_type: str, **_k: Any) -> UUID:
        new_id = uuid4()
        created.append(new_id)
        log.append(("create", entity_type))
        return new_id

    async def membership(session: Any, **kwargs: Any) -> UUID:
        log.append(("membership", (kwargs["entity_id"], kwargs["evidence_ref"].chunk_id)))
        return uuid4()

    async def derived(session: Any, **kwargs: Any) -> None:
        log.append(("derived", (kwargs["field_name"], kwargs["value"])))

    async def relation(session: Any, **kwargs: Any) -> UUID:
        log.append(
            (
                "relationship",
                (
                    kwargs["source_entity_id"],
                    kwargs["target_entity_id"],
                    kwargs["relationship_type"],
                ),
            )
        )
        return uuid4()

    async def finish(session: Any, *_a: Any, **kwargs: Any) -> bool:
        log.append(("finish", kwargs))
        return True

    target = entities_worker.entities
    monkeypatch.setattr(target, "list_resolution_candidates", candidates)
    monkeypatch.setattr(target, "get_document_correction_decisions", decide)
    monkeypatch.setattr(target, "get_entity_refs", get_refs)
    monkeypatch.setattr(
        target, "resolve_canonical_entity_id", AsyncMock(side_effect=lambda _s, i, **_k: i)
    )
    monkeypatch.setattr(target, "create_extracted_entity", create)
    monkeypatch.setattr(target, "record_extraction_membership", membership)
    monkeypatch.setattr(target, "publish_derived_field", derived)
    monkeypatch.setattr(target, "finish_extraction_work", finish)
    monkeypatch.setattr(entities_worker.documents, "read_extraction_evidence_refs", evidence)
    monkeypatch.setattr(entities_worker.relationships, "publish_extracted_relationship", relation)
    monkeypatch.setattr(
        entities_worker, "make_graph_change", lambda *, entity_id, scope: ("graph", entity_id)
    )
    replay = AsyncMock()
    monkeypatch.setattr(entities_worker, "commit_with_replay", replay)
    return _Run(m=m, log=log, created=created, replay=replay)


def _finish(run: _Run) -> dict[str, Any]:
    """Return the keyword arguments passed to ``finish_extraction_work``."""
    return next(payload for name, payload in run.log if name == "finish")


def _names(run: _Run, kind: str) -> list[Any]:
    """Return logged payloads of one collaborator kind in call order."""
    return [payload for name, payload in run.log if name == kind]


async def test_new_entities_publish_memberships_fields_and_relationship(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unmatched candidates become new entities with per-chunk memberships, derived fields and a published edge."""
    extracted = _output(
        [
            _entity("a", "Zelda Quill", [C1, C2], description="a hero"),
            _entity("b", "Orvin Pax", [C1]),
        ],
        [_relation("a", "b", C1)],
    )
    run = _setup(monkeypatch, extracted)
    await run.m.run()
    a_id, b_id = run.created
    assert _names(run, "evidence") == [sorted([C1, C2], key=str)]
    assert _names(run, "create") == ["person", "person"]
    assert _names(run, "membership") == [(a_id, C1), (a_id, C2), (b_id, C1)]
    assert _names(run, "derived") == [
        ("name", "Zelda Quill"),
        ("description", "a hero"),
        ("name", "Zelda Quill"),
        ("description", "a hero"),
        ("name", "Orvin Pax"),
    ]
    assert _names(run, "relationship") == [(a_id, b_id, "WORKS_WITH")]
    finish = _finish(run)
    assert finish["review"] == []
    assert [sorted(f) for f in finish["facts"][:2]] == [
        ["candidate_key", "confidence", "entity_id"]
    ] * 2
    assert finish["facts"][2]["relationship"] == "WORKS_WITH"
    assert finish["model"] == "actual-model" and finish["usage"] == {"tokens": 3}
    run.replay.assert_awaited_once()
    assert run.replay.await_args.args[1] == [("graph", i) for i in sorted({a_id, b_id}, key=str)]
    # Single transaction: only the pre-inference release rolls back and only commit_with_replay commits.
    assert run.m.outer().rollback.await_count == 1
    assert run.m.outer().commit.await_count == 0


async def test_exact_alias_match_reuses_entity_without_derived_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single confirmed-alias match locks the existing entity for write and publishes no new fields."""
    existing = uuid4()
    known = {
        "person": (
            [
                {
                    "id": existing,
                    "type": "person",
                    "name": "Zelda Quill",
                    "revision": 4,
                    "confirmed_aliases": [canonicalize_name("Zelda Quill")],
                }
            ],
            False,
        )
    }
    run = _setup(monkeypatch, _output([_entity("a", "Zelda Quill", [C1])]), known=known)
    await run.m.run()
    assert run.created == []
    assert _names(run, "refs") == [([existing], True)]
    assert _names(run, "derived") == []
    assert _names(run, "membership") == [(existing, C1)]
    # Names are listed once before and once after the entity lock closure; decisions refresh takes FOR UPDATE.
    assert _names(run, "candidates") == [("person", ["Zelda Quill"])] * 2
    assert _names(run, "decisions") == [False, True]


async def test_match_locks_are_taken_in_sorted_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """Existing entities are locked once, in str-sorted id order, regardless of candidate order."""
    ids = [uuid4(), uuid4(), uuid4()]
    known = {
        "person": (
            [
                {
                    "id": i,
                    "type": "person",
                    "name": f"Name {n}",
                    "revision": 1,
                    "confirmed_aliases": [canonicalize_name(f"Distinct{n}x")],
                }
                for n, i in enumerate(ids)
            ],
            False,
        )
    }
    entities_in = [_entity(f"k{n}", f"Distinct{n}x", [C1]) for n in (2, 0, 1)]
    run = _setup(monkeypatch, _output(entities_in), known=known)
    await run.m.run()
    assert _names(run, "refs") == [(sorted(ids, key=str), True)]


async def test_intra_response_near_duplicates_both_go_to_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two new same-type candidates with similar names are held for review and create nothing."""
    extracted = _output(
        [
            _entity("a", "Alexander Hamilton", [C1]),
            _entity("b", "Alexander Hamiltn", [C2]),
            _entity("c", "Totally Different", [C3]),
        ],
        [_relation("a", "c", C1)],
    )
    run = _setup(monkeypatch, extracted)
    await run.m.run()
    finish = _finish(run)
    reviews = {r["candidate_name"]: r for r in finish["review"] if r["kind"] == "entity"}
    assert set(reviews) == {"Alexander Hamilton", "Alexander Hamiltn"}
    assert all(
        r["reason"] == "ambiguous_identity" and r["possible_entity_ids"] == []
        for r in reviews.values()
    )
    assert len(run.created) == 1
    rel = [r for r in finish["review"] if r["kind"] == "relationship"]
    assert [r["reason"] for r in rel] == ["relationship_endpoint_requires_review"]
    assert rel[0]["possible_entity_ids"] == []
    assert len(rel[0]["snapshot_digest"]) == 64
    assert _names(run, "relationship") == []


async def test_new_vs_matched_near_duplicate_reviews_the_new_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A new candidate similar to a matched sibling is review-only; the refresh pass recomputes (and here empties) possible ids."""
    existing = uuid4()
    known = {
        "person": (
            [
                {
                    "id": existing,
                    "type": "person",
                    "name": "Other",
                    "revision": 1,
                    "confirmed_aliases": [canonicalize_name("Alexander Hamilton")],
                }
            ],
            False,
        )
    }
    extracted = _output(
        [_entity("a", "Alexander Hamilton", [C1]), _entity("b", "Alexander Hamiltn", [C2])]
    )
    run = _setup(monkeypatch, extracted, known=known)
    await run.m.run()
    review = _finish(run)["review"]
    assert [(r["candidate_name"], r["reason"], r["possible_entity_ids"]) for r in review] == [
        ("Alexander Hamiltn", "ambiguous_identity", [])
    ]


async def test_context_overflow_is_review_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bounded resolution context forces review with the dedicated reason and creates nothing."""
    run = _setup(monkeypatch, _output([_entity("a", "Zelda", [C1])]), known={"person": ([], True)})
    await run.m.run()
    assert run.created == []
    assert [r["reason"] for r in _finish(run)["review"]] == ["resolution_context_bounded"]


async def test_owner_suppress_and_assign_decisions(monkeypatch: pytest.MonkeyPatch) -> None:
    """Owner corrections override resolution: suppress reviews, assign matches the target entity."""
    target = uuid4()
    decision_ids = {"a": uuid4(), "b": uuid4()}

    def decisions(_args: Any) -> dict[str, tuple[UUID, str, UUID | None]]:
        return {
            "a": (decision_ids["a"], "suppress", None),
            "b": (decision_ids["b"], "assign", target),
        }

    extracted = _output([_entity("a", "Zelda Quill", [C1]), _entity("b", "Orvin Pax", [C2])])
    known = {
        "person": (
            [
                {
                    "id": target,
                    "type": "person",
                    "name": "Whoever",
                    "revision": 2,
                    "confirmed_aliases": [],
                }
            ],
            False,
        )
    }
    run = _setup(monkeypatch, extracted, known=known, decisions=decisions)
    await run.m.run()
    review = _finish(run)["review"]
    assert [(r["candidate_name"], r["reason"]) for r in review] == [
        ("Zelda Quill", "owner_suppressed_candidate")
    ]
    assert _names(run, "membership") == [(target, C2)]
    assert _names(run, "refs") == [([target], True)]


async def test_bad_relationships_are_dropped_not_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Self-loops and edges naming unlisted entities are discarded while valid entities still publish."""
    extracted = _output(
        [_entity("a", "Zelda Quill", [C1]), _entity("b", "Orvin Pax", [C1])],
        [_relation("a", "a", C1), _relation("a", "ghost", C1), _relation("a", "b", C1)],
    )
    run = _setup(monkeypatch, extracted)
    await run.m.run()
    assert len(_names(run, "relationship")) == 1
    assert _finish(run)["review"] == []


async def test_relationship_without_membership_for_chunk_is_ambiguous_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An edge whose evidence chunk has no membership for both endpoints is retained for review with endpoint ids."""
    extracted = _output(
        [_entity("a", "Zelda Quill", [C1]), _entity("b", "Orvin Pax", [C1])],
        [_relation("a", "b", C2)],
    )
    run = _setup(monkeypatch, extracted)
    await run.m.run()
    a_id, b_id = run.created
    review = _finish(run)["review"]
    assert [(r["kind"], r["reason"], r["possible_entity_ids"]) for r in review] == [
        ("relationship", "relationship_identity_ambiguous", [str(a_id), str(b_id)])
    ]


@pytest.mark.parametrize(
    ("which", "code"),
    [
        ("duplicate", "duplicate_candidate_key"),
        ("relationship_chunk", "unknown_evidence_chunk"),
        ("entity_chunk", "unknown_evidence_chunk"),
    ],
)
async def test_pure_response_checks_fail_before_relock(
    monkeypatch: pytest.MonkeyPatch, which: str, code: str
) -> None:
    """Invalid model output is rejected with a recorded code before the source is re-locked."""
    if which == "duplicate":
        extracted = _output([_entity("a", "Name A", [C1]), _entity("a", "Name B", [C1])])
    elif which == "relationship_chunk":
        extracted = _output(
            [_entity("a", "Name A", [C1]), _entity("b", "Name B", [C1])],
            [_relation("a", "b", uuid4())],
        )
    else:
        extracted = _output([_entity("a", "Name A", [uuid4()])])
    run = _setup(monkeypatch, extracted)
    await run.m.run()
    assert run.m.lock_source.await_count == 1
    assert run.m.set_error.await_args.args[3] == code
    assert run.m.set_error.await_args.kwargs["blocked"] is False
    assert _names(run, "finish") == []


async def test_non_dict_response_is_invalid_model_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-object provider reply is recorded as ``invalid_model_response`` (retryable)."""
    run = _setup(monkeypatch, _output([]))

    async def structured(self: Any, *_a: Any, **_k: Any) -> list[int]:
        return []

    monkeypatch.setattr(lock_tests._Gateway, "structured", structured)
    await run.m.run()
    assert run.m.set_error.await_args.args[3] == "invalid_model_response"


async def test_http_error_other_than_409_terminalizes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lost permission mid-run is terminal; a 409 keeps the retry budget with ``extraction_failed``."""
    run = _setup(monkeypatch, _output([_entity("a", "Zelda", [C1])]))
    terminalize = AsyncMock()
    monkeypatch.setattr(entities_worker, "terminalize", terminalize)
    monkeypatch.setattr(
        entities_worker.entities,
        "create_extracted_entity",
        AsyncMock(side_effect=HTTPException(403, "no")),
    )
    await run.m.run()
    terminalize.assert_awaited_once()
    assert terminalize.await_args.args[4] == entities_worker.PERMISSION_LOST
    run.m.set_error.assert_not_awaited()

    run2 = _setup(monkeypatch, _output([_entity("a", "Zelda", [C1])]))
    monkeypatch.setattr(
        entities_worker.entities,
        "create_extracted_entity",
        AsyncMock(side_effect=HTTPException(409, "x")),
    )
    await run2.m.run()
    assert run2.m.set_error.await_args.args[3] == "extraction_failed"


async def test_lost_lease_rolls_back_and_skips_replay(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the final lease acknowledgement fails the graph writes roll back and nothing is committed."""
    run = _setup(monkeypatch, _output([_entity("a", "Zelda", [C1])]))

    async def finish(session: Any, *_a: Any, **_k: Any) -> bool:
        return False

    monkeypatch.setattr(entities_worker.entities, "finish_extraction_work", finish)
    await run.m.run()
    run.replay.assert_not_awaited()
    assert run.m.outer().rollback.await_count == 2
