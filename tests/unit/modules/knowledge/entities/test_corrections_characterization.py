"""Golden characterization of entity correction discovery, locking and merge/split/delete write order.

Each scenario records the ordered SQL statement digests (compiled PostgreSQL text without parameters), ORM
add/delete/flush calls, and collaborator calls into a log and compares it with ``golden_corrections.json``.
Regenerate intentionally with ``REGEN_GOLDEN=1`` only when a behaviour change is approved.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest
from sqlalchemy.dialects import postgresql

from modules.knowledge.entities import corrections as cor
from modules.knowledge.entities.schemas import EntityCreate, EntityMergeRequest, EntitySplitRequest
from modules.knowledge.relationships.schemas import CorrectionRelationshipRef, CorrectionSupportRef
from tests.unit.modules.knowledge.entities._corrections_patch import patch_corrections_mp

GOLDEN_PATH = Path(__file__).with_name("golden_corrections.json")
NOW = datetime(2026, 5, 1, tzinfo=UTC)
WS = uuid5(NAMESPACE_URL, "ws")
LABELS: dict[UUID, str] = {}


def U(name: str) -> UUID:
    """Deterministic UUID that remembers its label for readable logs."""
    value = uuid5(NAMESPACE_URL, name)
    LABELS[value] = name
    return value


def label(value: Any) -> Any:
    """Replace known UUIDs inside simple structures with their labels."""
    if isinstance(value, UUID):
        return LABELS.get(value, str(value))
    if isinstance(value, SimpleNamespace):
        return f"<{label(getattr(value, 'id', None))}>"
    if isinstance(value, (list, tuple)):
        return [label(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(str(label(item)) for item in value)
    if isinstance(value, dict):
        return {str(label(k)): label(v) for k, v in value.items()}
    return value


def _digest(statement: Any) -> str:
    """Stable digest of the compiled PostgreSQL text of one statement."""
    text = str(statement.compile(dialect=postgresql.dialect()))
    return hashlib.sha1(text.encode()).hexdigest()[:10]  # noqa: S324  # fixture digest, not a security use


class Session:
    """Recording async session returning scripted results in call order."""

    def __init__(self, log: list[str], scripted: list[Any] | None = None) -> None:
        self.log = log
        self.queue = list(scripted or [])

    def _next(self, default: Any) -> Any:
        return self.queue.pop(0) if self.queue else default

    async def scalars(self, statement: Any) -> Any:
        self.log.append(f"scalars:{_digest(statement)}")
        batch = self._next([])
        return SimpleNamespace(all=lambda: list(batch))

    async def scalar(self, statement: Any) -> Any:
        self.log.append(f"scalar:{_digest(statement)}")
        return self._next(None)

    async def execute(self, statement: Any) -> Any:
        self.log.append(f"execute:{_digest(statement)}")
        return SimpleNamespace()

    def add(self, row: Any) -> None:
        self.log.append(f"add:{type(row).__name__}")

    def add_all(self, rows: list[Any]) -> None:
        for row in rows:
            self.add(row)

    async def delete(self, row: Any) -> None:
        self.log.append(f"delete:{type(row).__name__}:{label(getattr(row, 'id', None))}")

    async def flush(self) -> None:
        self.log.append("flush")


def ent(key: str, **over: Any) -> SimpleNamespace:
    """Entity row stand-in."""
    values: dict[str, Any] = {
        "id": U(key),
        "revision": 2,
        "type": "person",
        "name": key,
        "canonical_name": key.lower(),
        "description": None,
        "name_origin": "derived",
        "description_origin": None,
        "metadata_json": {},
    }
    values.update(over)
    return SimpleNamespace(**values)


def mem(name: str, entity: str, **over: Any) -> SimpleNamespace:
    """Evidence membership stand-in."""
    values: dict[str, Any] = {
        "id": U(name),
        "entity_id": U(entity),
        "document_id": U("doc"),
        "source_id": U("src"),
        "document_version_id": U("ver"),
        "chunk_id": U(f"chunk-{name}"),
        "extraction_identity": "x",
        "candidate_key": "k",
        "match_fingerprint": "f" * 64,
        "observed_at": NOW,
        "extracted_at": NOW,
        "confidence": 0.5,
    }
    values.update(over)
    return SimpleNamespace(**values)


def alias(name: str, entity: str, **over: Any) -> SimpleNamespace:
    """Alias row stand-in."""
    values: dict[str, Any] = {
        "id": U(name),
        "entity_id": U(entity),
        "normalized_alias": name.lower(),
        "alias": name,
        "source_id": None,
        "confirmed": False,
        "origin": "derived",
        "confidence": 0.4,
    }
    values.update(over)
    return SimpleNamespace(**values)


def ref(name: str, source: str, target: str) -> CorrectionRelationshipRef:
    """Relationship ref with one support."""
    return CorrectionRelationshipRef(
        id=U(name),
        source_entity_id=U(source),
        target_entity_id=U(target),
        type="KNOWS",
        origin="derived",
        valid_from=None,
        valid_to=None,
        metadata={},
        supports=[
            CorrectionSupportRef(
                id=U(f"{name}-sup"),
                document_id=U("doc2"),
                source_id=U("src2"),
                document_version_id=U("ver2"),
                chunk_id=U("chunk2"),
                source_membership_id=U("m-rel-s"),
                target_membership_id=U("m-rel-t"),
                confidence=0.3,
            )
        ],
    )


def summary(closure: Any) -> dict[str, Any]:
    """Ids carried by a closure dataclass (everything list-like, labelled)."""
    out: dict[str, Any] = {}
    for key, value in vars(closure).items():
        if isinstance(value, list):
            out[key] = [
                label(getattr(v, "id", v)) if not isinstance(v, tuple) else label(v) for v in value
            ]
    return out


def check(name: str, payload: Any) -> None:
    """Compare ``payload`` with the stored golden value (or write it with REGEN_GOLDEN=1)."""
    data = json.loads(GOLDEN_PATH.read_text()) if GOLDEN_PATH.exists() else {}
    normalized = json.loads(json.dumps(payload, default=str, sort_keys=True))
    if os.environ.get("REGEN_GOLDEN"):
        data[name] = normalized
        GOLDEN_PATH.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")
        return
    assert data[name] == normalized


SCOPE = SimpleNamespace(workspace_id=WS, user_id=7)


@pytest.fixture(autouse=True)
def _collaborators(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Patch every cross-module collaborator and expose the shared call log."""
    log: list[str] = []
    env = SimpleNamespace(log=log, refs=[], timeline_ids=[])

    def logged(name: str, value: Any = None) -> AsyncMock:
        async def call(*args: Any, **kwargs: Any) -> Any:
            detail = {
                k: label(v)
                for k, v in kwargs.items()
                if k not in {"scope", "multi_workspace_enabled", "access_fence"}
            }
            positional = (
                [label(a) for a in args[1:]]
                if args and hasattr(args[0], "queue")
                else [label(a) for a in args]
            )
            log.append(f"{name}:{json.dumps([positional, detail], default=str, sort_keys=True)}")
            return value(*args, **kwargs) if callable(value) else value

        return AsyncMock(side_effect=call)

    async def refs(session: Any, ids: list[Any], **_k: Any) -> list[Any]:
        log.append(f"rel_refs:{label(ids)}")
        return env.refs

    async def tl_ids(session: Any, ids: list[Any], **_k: Any) -> list[Any]:
        log.append(f"timeline_ids:{label(ids)}")
        return env.timeline_ids

    monkeypatch.setattr(cor.relationships, "list_correction_relationship_refs", refs)
    monkeypatch.setattr(cor.timeline, "correction_event_ids", tl_ids)
    patch_corrections_mp(monkeypatch, "uuid7", lambda: U("new-entity"))
    for module, names in (
        (cor.sources, ["lock_source"]),
        (cor.documents, ["lock_document_ids"]),
        (cor.entities, ["lock_entity_ids", "get_membership_refs", "record_owner_action"]),
        (
            cor.relationships,
            ["lock_delete_closure", "remove_entity_closure", "lock_correction_closure"],
        ),
        (cor.timeline, ["lock_event_ids", "remove_entity_participants", "revise_corrected_events"]),
    ):
        for attr in names:
            default: Any = None
            if attr == "lock_source":
                default = SimpleNamespace(id=U("src"))
            if attr == "lock_delete_closure":
                default = ([U("rel1")], [U("rel1-sup")])
            if attr == "remove_entity_participants" or attr == "revise_corrected_events":
                default = []
            monkeypatch.setattr(
                module, attr, logged(f"{module.__name__.split('.')[-2]}.{attr}", default)
            )
    patch_corrections_mp(monkeypatch, "_admit", logged("admit", "fence"))
    patch_corrections_mp(monkeypatch, "_actor", lambda _scope: 7)
    patch_corrections_mp(monkeypatch, "_temporal_before_relationships", logged("temporal_before"))
    patch_corrections_mp(monkeypatch, "_temporal_correction", logged("temporal_correction"))
    patch_corrections_mp(monkeypatch, "_record_assignments", logged("record_assignments"))
    patch_corrections_mp(monkeypatch, "commit_with_replay", logged("commit"))
    patch_corrections_mp(
        monkeypatch,
        "make_graph_change",
        lambda **k: (
            f"change:{json.dumps(label({n: v for n, v in k.items() if n != 'scope'}), default=str, sort_keys=True)}"
        ),
    )
    monkeypatch.setattr(
        cor.entities, "resolve_canonical_entity_id", AsyncMock(side_effect=lambda _s, i, **_k: i)
    )
    return env


async def discover(env: SimpleNamespace, scripted: list[Any], ids: list[str], **kw: Any) -> Any:
    """Run ``_discover`` against a scripted session."""
    session = Session(env.log, scripted)
    return await cor._discover(session, [U(i) for i in ids], scope=SCOPE, **kw)


async def conflict_of(coro: Any) -> Any:
    """Return the structured conflict raised by ``coro`` (or fail)."""
    with pytest.raises(cor.CorrectionConflictError) as failure:
        await coro
    return failure.value.conflict.model_dump(mode="json")


async def test_discover_minimal(_collaborators: SimpleNamespace) -> None:
    """One entity without rows: queries run in a fixed order and an empty closure results."""
    closure = await discover(_collaborators, [[ent("a")]], ["a"])
    check("discover_minimal", {"log": _collaborators.log, "closure": summary(closure)})


async def test_discover_pair_with_aliases_redirect_and_neighbors(
    _collaborators: SimpleNamespace,
) -> None:
    """Merge-style discovery with target memberships, aliases/supports, redirect child, neighbor expansion."""
    a, b, c, old = ent("a"), ent("b"), ent("c"), ent("old")
    a_mem, b_mem = mem("m-a", "a"), mem("m-b", "b")
    al = alias("Alpha", "a")
    al_sup = SimpleNamespace(id=U("alsup"), alias_id=al.id, membership_id=a_mem.id, confidence=0.1)
    f_sup = SimpleNamespace(
        id=U("fsup"), entity_id=a.id, field_name="name", value_hash="h", membership_id=b_mem.id
    )
    redirect = SimpleNamespace(old_entity_id=old.id, target_entity_id=a.id, actor_id=1, reason="r")
    _collaborators.refs = [ref("rel1", "a", "c")]
    _collaborators.timeline_ids = [U("ev1")]
    scripted = [
        [a, b],
        [a_mem],
        [b_mem],
        [al],
        [al_sup],
        [f_sup],
        [redirect],
        [],
        [],
        [],
        [],  # retained probes (none) then (redirect frontier empty)
        [a, b, c, old],
    ]
    closure = await discover(_collaborators, scripted, ["a", "b"], include_target_memberships=True)
    check("discover_pair", {"log": _collaborators.log, "closure": summary(closure)})


@pytest.mark.parametrize(
    "case", ["missing", "memberships", "aliases", "support", "outside", "cycle", "retained"]
)
async def test_discover_conflicts(_collaborators: SimpleNamespace, case: str) -> None:
    """Every bound or consistency failure raises its structured conflict at a fixed query position."""
    a = ent("a")
    a_mem = mem("m-a", "a")
    al = alias("Alpha", "a")
    if case == "missing":
        scripted: list[Any] = [[], []]
    elif case == "memberships":
        scripted = [[a], [mem(f"m{i}", "a") for i in range(201)]]
    elif case == "aliases":
        scripted = [[a], [], [alias(f"a{i}", "a") for i in range(201)]]
    elif case == "support":
        scripted = [
            [a],
            [a_mem],
            [al],
            [
                SimpleNamespace(id=U(f"s{i}"), alias_id=al.id, membership_id=a_mem.id, confidence=1)
                for i in range(201)
            ],
        ]
    elif case == "outside":
        f_sup = SimpleNamespace(
            id=U("fs"),
            entity_id=a.id,
            field_name="name",
            value_hash="h",
            membership_id=U("unknown"),
        )
        scripted = [[a], [a_mem], [], [f_sup]]
    elif case == "cycle":
        cyc = SimpleNamespace(old_entity_id=a.id, target_entity_id=a.id, actor_id=1, reason="r")
        scripted = [[a], [a_mem], [], [], [cyc]]
    else:
        child = SimpleNamespace(
            old_entity_id=U("old"), target_entity_id=a.id, actor_id=1, reason="r"
        )
        scripted = [[a], [a_mem], [], [], [child], [], [], [U("kept-membership")], [], []]
    conflict = await conflict_of(discover(_collaborators, scripted, ["a"]))
    check(f"discover_conflict_{case}", {"log": _collaborators.log, "conflict": conflict})


def delete_closure_rows() -> list[Any]:
    """Scripted results for a small canonical-deletion discovery."""
    root, old, other = ent("root"), ent("old"), ent("other")
    redirect = SimpleNamespace(
        old_entity_id=old.id, target_entity_id=root.id, actor_id=1, reason="r"
    )
    root_mem = mem("m-root", "root")
    al = alias("Alias", "root")
    decision = SimpleNamespace(
        id=U("dec"),
        decision="assign",
        scope="evidence",
        entity_id=root.id,
        document_id=None,
        membership_id=root_mem.id,
        match_fingerprint="f",
        actor_id=1,
        reason="r",
        created_at=NOW,
    )
    al_sup = SimpleNamespace(
        id=U("alsup"), alias_id=al.id, membership_id=root_mem.id, confidence=0.1
    )
    f_sup = SimpleNamespace(
        id=U("fsup"),
        entity_id=root.id,
        field_name="name",
        value_hash="h",
        membership_id=root_mem.id,
    )
    return [
        root,
        [redirect],
        [],
        [redirect],
        [root, old],
        [root_mem],
        [al],
        [decision],
        [al_sup],
        [f_sup],
        [other, root, old],
    ]


async def test_discover_delete_closure(_collaborators: SimpleNamespace) -> None:
    """Deletion discovery reads redirects, owned rows, supports, incident edges and locks neighbors in order."""
    _collaborators.refs = [ref("rel1", "root", "other")]
    _collaborators.timeline_ids = [U("ev1")]
    scripted = delete_closure_rows()
    closure = await cor._discover_delete_closure(
        Session(_collaborators.log, scripted), U("root"), scope=SCOPE
    )
    check("delete_closure", {"log": _collaborators.log, "closure": summary(closure)})


@pytest.mark.parametrize("case", ["missing", "redirected", "deleted", "too_large", "changed"])
async def test_discover_delete_closure_conflicts(
    _collaborators: SimpleNamespace, case: str
) -> None:
    """Missing, redirected, oversized and disappearing closures raise stable conflicts."""
    root, old = ent("root"), ent("old")
    if case == "missing":
        scripted: list[Any] = [None]
    elif case == "redirected":
        scripted = [root, [], [SimpleNamespace(old_entity_id=root.id, target_entity_id=U("x"))]]
    elif case == "deleted":
        scripted = [root, [], [SimpleNamespace(old_entity_id=root.id, target_entity_id=None)]]
    elif case == "too_large":
        scripted = [root, [], [], [], [root], [mem(f"m{i}", "root") for i in range(201)], [], []]
    else:
        scripted = [
            root,
            [SimpleNamespace(old_entity_id=old.id, target_entity_id=root.id)],
            [],
            [],
            [root],
        ]
    conflict = await conflict_of(
        cor._discover_delete_closure(Session(_collaborators.log, scripted), U("root"), scope=SCOPE)
    )
    check(f"delete_conflict_{case}", {"log": _collaborators.log, "conflict": conflict})


def decision_row(name: str) -> SimpleNamespace:
    """Correction decision stand-in used in delete signatures."""
    return SimpleNamespace(
        id=U(name),
        decision="assign",
        scope="evidence",
        entity_id=U("root"),
        document_id=None,
        membership_id=U("m-root"),
        match_fingerprint="f",
        actor_id=1,
        reason="r",
        created_at=NOW,
    )


async def test_delete_canonical_entity_write_order(
    _collaborators: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Locks (sources, documents, entities, redirects, edges, events), re-discovery twice, then deletes and commit."""
    root, old = ent("root"), ent("old")
    redirect = SimpleNamespace(
        old_entity_id=old.id, target_entity_id=root.id, actor_id=1, reason="r"
    )
    root_mem = mem("m-root", "root")
    al = alias("Alias", "root")
    closure = cor._DeleteClosure(
        [root, old],
        [redirect],
        [root_mem],
        [al],
        [SimpleNamespace(id=U("alsup"), alias_id=al.id, membership_id=root_mem.id, confidence=0.1)],
        [
            SimpleNamespace(
                id=U("fsup"),
                entity_id=root.id,
                field_name="name",
                value_hash="h",
                membership_id=root_mem.id,
            )
        ],
        [decision_row("dec")],
        [ref("rel1", "root", "old")],
        [(U("ver"), U("chunk-m-root"))],
        [U("src")],
        [U("doc")],
        [U("old"), U("root")],
        [U("ev1")],
    )
    calls = {"n": 0}

    async def discover_delete(session: Any, entity_id: Any, **_k: Any) -> Any:
        calls["n"] += 1
        _collaborators.log.append(f"discover_delete:{calls['n']}")
        return closure

    patch_corrections_mp(monkeypatch, "_discover_delete_closure", discover_delete)
    session = Session(_collaborators.log)
    session.delete = Session.delete.__get__(session)  # type: ignore[method-assign]
    result = await cor.delete_canonical_entity(
        session, U("root"), scope=SCOPE, multi_workspace_enabled=False, reason="why"
    )
    state = {
        "root_revision": root.revision,
        "old_revision": old.revision,
        "old_name": old.name,
        "redirect_target": redirect.target_entity_id,
    }
    check(
        "delete_write_order", {"log": _collaborators.log, "result": result, "state": label(state)}
    )


def merge_closure() -> Any:
    """Closure for merging ``src`` into ``tgt`` with colliding and movable aliases and supports."""
    src = ent(
        "src",
        name_origin="owner",
        name="Source Name",
        canonical_name="source name",
        description="d",
        description_origin="derived",
    )
    tgt = ent("tgt", name_origin="derived", name="Target Name", canonical_name="target name")
    memberships = [mem("m-src", "src"), mem("m-tgt", "tgt")]
    src_alias_new = alias("Fresh", "src")
    src_alias_dup = alias("Dup", "src", alias="Dup")
    tgt_alias_dup = alias("Dup", "tgt", id=U("tgt-dup"), alias="Dup")
    src_dup_support = SimpleNamespace(
        id=U("dupsup"), alias_id=src_alias_dup.id, membership_id=memberships[0].id, confidence=0.9
    )
    tgt_dup_support = SimpleNamespace(
        id=U("tgtsup"), alias_id=tgt_alias_dup.id, membership_id=memberships[1].id, confidence=0.2
    )
    fresh_support = SimpleNamespace(
        id=U("freshsup"), alias_id=src_alias_new.id, membership_id=memberships[0].id, confidence=0.4
    )
    name_hash = hashlib.sha256(b"Target Name").hexdigest()
    field_supports = [
        SimpleNamespace(
            id=U("fs1"),
            entity_id=src.id,
            field_name="name",
            value_hash=name_hash,
            membership_id=memberships[0].id,
        ),
        SimpleNamespace(
            id=U("fs2"),
            entity_id=src.id,
            field_name="description",
            value_hash="zz",
            membership_id=memberships[0].id,
        ),
    ]
    return (
        SimpleNamespace(
            entity_rows=[src, tgt],
            memberships=memberships,
            aliases=[src_alias_new, src_alias_dup, tgt_alias_dup],
            alias_supports=[src_dup_support, tgt_dup_support, fresh_support],
            field_supports=field_supports,
            relationship_refs=[ref("rel1", "src", "x")],
            redirect_rows=[SimpleNamespace(old_entity_id=U("old"), target_entity_id=src.id)],
            relationship_entity_ids=[U("src"), U("tgt")],
            entity_ids=[U("src"), U("tgt")],
            evidence_pairs=[],
            source_ids=[],
            document_ids=[],
            membership_ids=[m.id for m in memberships],
            timeline_event_ids=[U("ev1")],
            relationship_ids=[U("rel1")],
        ),
        src,
        tgt,
        tgt_alias_dup,
        src_alias_dup,
        src_alias_new,
    )


async def test_merge_entity_write_order(
    _collaborators: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Merge validates, folds fields/aliases/supports, redirects, audits and commits in one fixed order."""
    closure, src, tgt, _tgt_dup, _src_dup, src_new = merge_closure()

    async def locked(session: Any, ids: list[Any], **kwargs: Any) -> Any:
        _collaborators.log.append(
            f"locked_closure:{label(ids)}:{kwargs.get('include_target_memberships')}"
        )
        return closure

    async def rel_merge(session: Any, *args: Any, **_k: Any) -> Any:
        _collaborators.log.append("apply_entity_merge_rel")
        return [(U("rel1"), U("rel1-new"))]

    async def tl_merge(session: Any, **_k: Any) -> Any:
        _collaborators.log.append("apply_entity_merge_tl")
        return [U("ev1")]

    patch_corrections_mp(monkeypatch, "_locked_closure", locked)
    monkeypatch.setattr(cor.relationships, "apply_entity_merge", rel_merge)
    monkeypatch.setattr(cor.timeline, "apply_entity_merge", tl_merge)
    monkeypatch.setattr(
        cor.relationships,
        "validate_entity_merge_plan",
        lambda *a: _collaborators.log.append("validate_merge_plan"),
    )
    session = Session(_collaborators.log)
    payload = EntityMergeRequest(
        into_id=U("tgt"), expected_revision=2, expected_into_revision=2, reason="merge it"
    )
    result = await cor.merge_entity(
        session, U("src"), payload, scope=SCOPE, multi_workspace_enabled=False
    )
    state = {
        "src": [src.revision, src.name, src.name_origin],
        "tgt": [tgt.revision, tgt.name, tgt.name_origin, tgt.canonical_name],
        "memberships": [(m.id, m.entity_id) for m in closure.memberships],
        "alias_entities": {a.id: a.entity_id for a in (src_new,)},
        "alias_supports": [(s.id, s.alias_id, s.confidence) for s in closure.alias_supports],
        "field_supports": [(s.id, s.entity_id) for s in closure.field_supports],
        "redirects": [r.target_entity_id for r in closure.redirect_rows],
    }
    check(
        "merge_write_order",
        {
            "log": _collaborators.log,
            "result": result.model_dump(mode="json"),
            "state": label(state),
        },
    )


async def test_split_entity_write_order(
    _collaborators: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Split creates the owner entity, moves selected evidence/supports, prunes derived leftovers, audits, commits."""
    src = ent("src", name_origin="derived", description="desc", description_origin="derived")
    selected = mem("m-sel", "src")
    other = mem("m-other", "src")
    owner_alias = alias("OwnerAlias", "src", origin="owner")
    derived_alias = alias("DerivedAlias", "src", origin="derived")
    closure = SimpleNamespace(
        entity_rows=[src],
        memberships=[selected, other],
        aliases=[owner_alias, derived_alias],
        alias_supports=[
            SimpleNamespace(
                id=U("s-owner"), alias_id=owner_alias.id, membership_id=selected.id, confidence=1.0
            ),
            SimpleNamespace(
                id=U("s-derived"),
                alias_id=derived_alias.id,
                membership_id=selected.id,
                confidence=0.5,
            ),
        ],
        field_supports=[
            SimpleNamespace(
                id=U("f-name"),
                entity_id=src.id,
                field_name="name",
                value_hash="h",
                membership_id=selected.id,
            ),
            SimpleNamespace(
                id=U("f-desc"),
                entity_id=src.id,
                field_name="description",
                value_hash="h",
                membership_id=selected.id,
            ),
        ],
        relationship_refs=[ref("rel1", "src", "x")],
        redirect_rows=[],
        relationship_entity_ids=[U("src")],
        entity_ids=[U("src")],
        evidence_pairs=[],
        source_ids=[],
        document_ids=[],
        membership_ids=[],
        timeline_event_ids=[U("ev1")],
        relationship_ids=[U("rel1")],
    )

    async def locked(session: Any, ids: list[Any], **_k: Any) -> Any:
        _collaborators.log.append(f"locked_closure:{label(ids)}")
        return closure

    async def rel_split(session: Any, *args: Any, **_k: Any) -> Any:
        _collaborators.log.append("apply_entity_split_rel")
        return [(U("rel1"), U("rel1-new"))]

    async def tl_split(session: Any, **_k: Any) -> Any:
        _collaborators.log.append("apply_entity_split_tl")
        return [U("ev1")]

    patch_corrections_mp(monkeypatch, "_locked_closure", locked)
    monkeypatch.setattr(cor.relationships, "apply_entity_split", rel_split)
    monkeypatch.setattr(cor.timeline, "apply_entity_split", tl_split)
    monkeypatch.setattr(
        cor.relationships,
        "validate_entity_split_plan",
        lambda *a: _collaborators.log.append("validate_split_plan"),
    )
    session = Session(_collaborators.log, [[], [], []])
    payload = EntitySplitRequest(
        evidence_ids=[selected.id],
        expected_revision=2,
        reason="split it",
        new_entity=EntityCreate(
            type="person", name="Newbie", aliases=["Extra", "Newbie"], description="nd"
        ),
    )
    result = await cor.split_entity(
        session, U("src"), payload, scope=SCOPE, multi_workspace_enabled=False
    )
    state = {
        "src": [src.revision, src.name, src.name_origin, src.description, src.description_origin],
        "memberships": [(m.id, m.entity_id) for m in closure.memberships],
        "alias_supports": [(s.id, s.alias_id) for s in closure.alias_supports],
    }
    check(
        "split_write_order",
        {
            "log": _collaborators.log,
            "result": result.model_dump(mode="json"),
            "state": label(state),
        },
    )


async def test_merge_aliases_deletes_the_merged_alias_support_on_membership_collision(
    _collaborators: SimpleNamespace,
) -> None:
    """Regression: a duplicate alias support colliding with the target's is merged into it and itself deleted.

    The pre-split code deleted an unrelated variable (``support``) here, raising ``UnboundLocalError`` when no
    earlier loop had bound it; the merged-away ``alias_support`` must be the row that is deleted.
    """
    membership = mem("m-shared", "src")
    source_alias = alias("Dup", "src")
    target_alias = alias("Dup", "tgt", id=U("tgt-dup"))
    moved = SimpleNamespace(
        id=U("moved"), alias_id=source_alias.id, membership_id=membership.id, confidence=0.9
    )
    kept = SimpleNamespace(
        id=U("kept"), alias_id=target_alias.id, membership_id=membership.id, confidence=0.2
    )
    closure = SimpleNamespace(alias_supports=[moved, kept])
    session = Session(_collaborators.log)
    await cor._merge_aliases(
        session,
        closure,
        [source_alias],
        {source_alias.normalized_alias: target_alias},
        ent("tgt"),
        [membership],  # type: ignore[arg-type]
    )
    assert kept.confidence == 0.9
    assert _collaborators.log == ["delete:SimpleNamespace:moved", "delete:SimpleNamespace:Dup"]
