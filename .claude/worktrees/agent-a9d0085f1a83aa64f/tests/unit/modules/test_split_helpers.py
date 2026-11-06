"""Characterization tests for helpers extracted from the >100-line misc-module functions.

They pin the pure decision logic (no database): the verdict of each branch must stay the same as
the pre-split inline code, so the splits remain behaviour-neutral.
"""

import hashlib
from datetime import date
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4, uuid5

import pytest
from sqlalchemy.dialects import postgresql

from core.errors import ConflictError
from modules.dashboard import briefs as dashboard_briefs
from modules.goals import public as goals
from modules.goals.schemas import PlanProposal
from modules.model_gateway import routes as gateway_routes
from modules.notifications import public as notifications
from modules.tasks import public as tasks
from modules.tasks.schemas import TaskFilter


def _sql(statement: Any) -> str:
    return str(
        statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )


# --- notifications: highlight row classification --------------------------------------------------

DOC, VERSION, DEFINITION, RULE = uuid4(), uuid4(), uuid4(), uuid4()
FINGERPRINT = "a" * 64


def _row(**overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "kind": "dashboard_highlight",
        "copied_evidence_revoked": False,
        "document_id": None,
        "document_version_id": None,
        "dedupe_key": f"highlight:{DEFINITION}:1:{FINGERPRINT}:{RULE}:{VERSION}",
        "params": {"definition_id": str(DEFINITION), "definition_revision": 1, "severity": "high"},
    }
    values.update(overrides)
    return SimpleNamespace(**values)


async def _classify(
    row: SimpleNamespace, known: set[UUID] | None = None
) -> tuple[str, UUID | None]:
    return await notifications._classify_highlight_row(
        None,
        row,
        DOC,
        known or set(),
        scope=None,
        multi_workspace_enabled=False,  # type: ignore[arg-type]
    )


async def test_classify_skips_other_kinds_revoked_and_foreign_documents() -> None:
    assert await _classify(_row(kind="brief.ready")) == ("skip", None)
    assert await _classify(_row(copied_evidence_revoked=True)) == ("skip", None)
    assert await _classify(_row(document_id=uuid4(), document_version_id=VERSION)) == ("skip", None)


async def test_classify_exact_sidecar_scrubs_and_flags_contradictions() -> None:
    assert await _classify(_row(document_id=DOC, document_version_id=VERSION)) == ("scrub", VERSION)
    other = uuid4()
    assert await _classify(_row(document_id=DOC, document_version_id=other)) == (
        "undecidable",
        None,
    )


async def test_classify_legacy_row_needs_a_known_version_receipt() -> None:
    assert await _classify(_row(), known={VERSION}) == ("scrub", VERSION)
    assert await _classify(_row(document_version_id=uuid4()), known={VERSION}) == (
        "undecidable",
        None,
    )


async def test_classify_unparseable_legacy_rows() -> None:
    # A legacy row with no sidecar and an unparseable key is genuinely undecidable.
    assert await _classify(_row(dedupe_key="highlight:garbage")) == ("undecidable", None)
    assert await _classify(_row(dedupe_key="highlight:garbage", document_id=DOC)) == (
        "undecidable",
        None,
    )


# --- dashboard briefs: manifest validation ---------------------------------------------------------


def _evidence(ref: int, index: int, **overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "fact_ref": ref,
        "support_index": index,
        "fact_kind": "stories",
        "fact_id": str(uuid4()),
        "fact_hash": "b" * 64,
        "document_id": uuid4(),
        "document_version_id": uuid4(),
        "chunk_id": uuid4(),
        "source_id": uuid4(),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_fact_group_accumulates_sources_and_documents() -> None:
    first = _evidence(1, 0)
    second = _evidence(1, 1, fact_id=first.fact_id)
    seen: set[tuple[str, str, str]] = set()
    sources: set[UUID] = set()
    documents: set[UUID] = set()
    assert dashboard_briefs._validate_fact_group([first, second], seen, sources, documents)
    assert len(seen) == 2 and sources == {first.source_id, second.source_id}
    assert documents == {first.document_id, second.document_id}


@pytest.mark.parametrize(
    "build",
    [
        lambda: [_evidence(1, 0, fact_kind="widgets")],
        lambda: [_evidence(1, 0, fact_hash="XYZ")],
        lambda: [_evidence(1, 1)],  # support indexes must start at 0
        lambda: [_evidence(1, 0, fact_id="not-a-uuid")],
        lambda: [
            _evidence(
                1, 0, document_id=None, document_version_id=None, chunk_id=None, source_id=None
            )
        ],  # story without support
        lambda: [_evidence(1, 0, document_version_id=None)],  # partial tuple
    ],
)
def test_fact_group_rejects_malformed_groups(build: Any) -> None:
    assert not dashboard_briefs._validate_fact_group(build(), set(), set(), set())


def test_independent_marker_is_valid_for_tasks_only() -> None:
    marker = {"document_id": None, "document_version_id": None, "chunk_id": None, "source_id": None}
    assert dashboard_briefs._validate_fact_group(
        [_evidence(1, 0, fact_kind="tasks", **marker)], set(), set(), set()
    )


def test_citations_must_match_captured_facts() -> None:
    fact = _evidence(1, 0)
    grouped = {1: [fact]}
    citation = {
        "ref": 1,
        "kind": "stories",
        "id": fact.fact_id,
        "title": "T",
        "source_ids": [str(fact.source_id)],
    }
    row = SimpleNamespace(citations=[citation])
    assert dashboard_briefs._validate_citations(row, grouped) == {1: citation}  # type: ignore[arg-type]
    for bad in (
        {**citation, "source_ids": [str(uuid4())]},
        {**citation, "kind": "tasks"},
        {**citation, "extra": 1},
        {**citation, "ref": 2},
    ):
        assert (
            dashboard_briefs._validate_citations(SimpleNamespace(citations=[bad]), grouped) is None
        )  # type: ignore[arg-type]
    assert dashboard_briefs._validate_citations(SimpleNamespace(citations=[]), grouped) is None  # type: ignore[arg-type]
    assert (
        dashboard_briefs._validate_citations(
            SimpleNamespace(citations=[citation, citation]), grouped
        )
        is None
    )  # type: ignore[arg-type]


def test_evidence_rows_one_marker_or_one_per_support() -> None:
    brief_id = uuid4()
    independent = {
        "kind": "tasks",
        "id": str(uuid4()),
        "_fact_hash": "c" * 64,
        "_lineage_status": "independent",
    }
    rows = dashboard_briefs._evidence_rows_for_fact(brief_id, 3, independent)
    assert [(r.fact_ref, r.support_index, r.document_id) for r in rows] == [(3, 0, None)]
    supports = [
        {
            "document_id": str(uuid4()),
            "document_version_id": str(uuid4()),
            "chunk_id": str(uuid4()),
            "source_id": str(uuid4()),
        }
        for _ in range(2)
    ]
    supported = {
        **independent,
        "kind": "stories",
        "_lineage_status": "captured",
        "_supports": supports,
    }
    rows = dashboard_briefs._evidence_rows_for_fact(brief_id, 1, supported)
    assert [r.support_index for r in rows] == [0, 1]
    assert str(rows[1].chunk_id) == supports[1]["chunk_id"]


# --- goals: proposal bounds ---------------------------------------------------------------------


def _proposal(**overrides: Any) -> PlanProposal:
    data: dict[str, Any] = {
        "proposal_id": "p1",
        "expected_revision": 1,
        "milestones": [],
        "tasks": [],
    }
    data.update(overrides)
    return PlanProposal.model_validate(data)


def test_plan_bounds_derive_stable_milestone_ids() -> None:
    goal = SimpleNamespace(id=uuid4(), revision=1, milestones=[], entity_ids=[])
    proposal = _proposal(milestones=[{"title": "m", "order": 0}, {"title": "n", "order": 1}])
    milestone_ids, entities = goals._check_plan_bounds(goal, proposal, [])  # type: ignore[arg-type]
    assert milestone_ids == [uuid5(goal.id, f"accepted:p1:milestone:{i}") for i in range(2)]
    assert entities == []


def test_plan_bounds_reject_existing_milestone_and_bad_task_index() -> None:
    goal_id = uuid4()
    taken = uuid5(goal_id, "accepted:p1:milestone:0")
    goal = SimpleNamespace(id=goal_id, revision=4, milestones=[{"id": str(taken)}], entity_ids=[])
    with pytest.raises(goals.GoalConflict) as caught:
        goals._check_plan_bounds(goal, _proposal(milestones=[{"title": "m", "order": 0}]), [])  # type: ignore[arg-type]
    assert caught.value.code == "milestone_id_conflict" and caught.value.current_revision == 4
    empty = SimpleNamespace(id=goal_id, revision=1, milestones=[], entity_ids=[])
    with pytest.raises(ValueError, match="task_index"):
        goals._check_plan_bounds(
            empty, _proposal(milestones=[{"title": "m", "order": 0, "task_index": 0}]), []
        )  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="1000"):
        goals._check_plan_bounds(empty, _proposal(), [{}] * 1000)  # type: ignore[arg-type]


async def test_replay_rejects_changed_content_for_same_proposal_id() -> None:
    goal = SimpleNamespace(id=uuid4(), revision=2)
    with pytest.raises(goals.GoalConflict) as caught:
        await goals._replay_accepted_plan(
            None,
            goal,
            {"content_hash": "old"},
            "new",
            scope=None,
            multi_workspace_enabled=False,  # type: ignore[arg-type]
        )
    assert caught.value.code == "idempotency_key_reused"


# --- tasks: view and filter composition -------------------------------------------------------------


def test_task_views_compose_expected_predicates() -> None:
    from datetime import UTC, datetime

    from sqlalchemy import select

    start, end = datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 2, tzinfo=UTC)
    base = select(tasks.Task)
    sql = {
        view: _sql(tasks._apply_task_view(base, view, date(2026, 1, 1), start, end))
        for view in ("inbox", "today", "upcoming", "blocked", "completed", None)
    }
    assert "tasks.status = 'inbox'" in sql["inbox"]
    assert (
        "NOT IN ('done', 'cancelled')" in sql["today"]
        and "tasks.due_date = '2026-01-01'" in sql["today"]
    )
    assert "tasks.due_date > '2026-01-01'" in sql["upcoming"]
    assert (
        "tasks.status = 'blocked'" in sql["blocked"] and "tasks.status = 'done'" in sql["completed"]
    )
    assert "WHERE" not in sql[None]


def test_task_filters_ignore_blank_text_and_apply_ranges() -> None:
    from sqlalchemy import select

    base = select(tasks.Task)
    assert "WHERE" not in _sql(tasks._apply_task_filters(base, TaskFilter(q="   ")))
    sql = _sql(
        tasks._apply_task_filters(
            base, TaskFilter(status="todo", due_date_from=date(2026, 1, 1), q=" x ")
        )
    )
    assert (
        "tasks.status = 'todo'" in sql and "tasks.due_date >= '2026-01-01'" in sql and "%x%" in sql
    )


def test_task_patch_sets_completion_only_for_done() -> None:
    from datetime import UTC, datetime

    task = SimpleNamespace(
        title="a",
        description=None,
        status="todo",
        goal_id=None,
        entity_ids=[],
        due_date=None,
        due_at=None,
        completed_at=None,
    )
    payload = SimpleNamespace(model_fields_set={"title"}, title="b", completed_at=None)
    tasks._apply_task_patch(
        task,
        payload,
        next_status="done",
        next_goal_id=None,
        next_entity_ids=None,  # type: ignore[arg-type]
        next_due_date=None,
        next_due_at=None,
    )
    assert task.title == "b" and task.status == "done" and task.completed_at is not None
    stamp = task.completed_at
    tasks._apply_task_patch(
        task,
        payload,
        next_status="done",
        next_goal_id=None,
        next_entity_ids=None,  # type: ignore[arg-type]
        next_due_date=None,
        next_due_at=None,
    )
    assert task.completed_at == stamp  # an existing completion time is kept
    tasks._apply_task_patch(
        task,
        payload,
        next_status="todo",
        next_goal_id=None,
        next_entity_ids=None,  # type: ignore[arg-type]
        next_due_date=None,
        next_due_at=None,
    )
    assert task.completed_at is None
    assert isinstance(stamp, datetime) and stamp.tzinfo is UTC


# --- model gateway: capability probe dispatch ---------------------------------------------------


class _FakeClient:
    """Records which gateway method the probe dispatched to."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def embed(self, *args: Any, **kwargs: Any) -> str:
        self.calls.append("embed")
        return "e"

    async def rerank(self, *args: Any, **kwargs: Any) -> str:
        self.calls.append("rerank")
        return "r"

    async def structured(self, *args: Any, **kwargs: Any) -> str:
        self.calls.append("structured")
        return "s"

    async def tools(self, *args: Any, **kwargs: Any) -> str:
        self.calls.append("tools")
        return "t"

    async def chat(self, *args: Any, **kwargs: Any) -> str:
        self.calls.append("chat")
        return "c"

    async def stream(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append("stream")
        for line in (
            'data: {"choices":[{"delta":{"content":"he"}}]}',
            "data: nope",
            'data: {"choices":[{"delta":{"content":"llo"}}]}',
            "data: [DONE]",
            'data: {"choices":[{"delta":{"content":"IGNORED"}}]}',
        ):
            yield line


@pytest.mark.parametrize(
    ("capability", "expected"),
    [
        ("embeddings", "embed"),
        ("reranking", "rerank"),
        ("structured", "structured"),
        ("tools", "tools"),
        ("chat", "chat"),
        ("anything-else", "chat"),
    ],
)
async def test_probe_dispatches_each_capability(capability: str, expected: str) -> None:
    client = _FakeClient()
    await gateway_routes._run_capability_probe(client, capability, "chat", None, None)  # type: ignore[arg-type]
    assert client.calls == [expected]


async def test_probe_streaming_collects_deltas_until_done() -> None:
    client = _FakeClient()
    result = await gateway_routes._run_capability_probe(client, "streaming", "chat", None, None)  # type: ignore[arg-type]
    assert result == {"streamed": "hello"}


# --- search: orchestration of the split indexing run -----------------------------------------------


async def test_index_generation_loop_stops_on_claim_or_process_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from modules.search import indexing

    ctx = object()
    claims = iter([object(), object(), object()])
    processed: list[object] = []

    async def prepare(*args: Any) -> tuple[Any, int | None]:
        return ctx, None

    async def claim(c: Any, state: Any) -> Any:
        return next(claims)

    async def process(c: Any, state: Any, claimed: Any) -> bool:
        processed.append(claimed)
        state.completed += 1
        return len(processed) < 2  # stop after the second chunk

    monkeypatch.setattr(indexing, "_prepare_index_run", prepare)
    monkeypatch.setattr(indexing, "_claim_next_chunk", claim)
    monkeypatch.setattr(indexing, "_process_claimed_chunk", process)
    assert await indexing._index_generation(None, None, None, uuid4(), uuid4()) == 2  # type: ignore[arg-type]


async def test_index_generation_returns_early_value_and_idle_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from modules.search import indexing

    async def failed(*args: Any) -> tuple[None, int]:
        return None, 0

    monkeypatch.setattr(indexing, "_prepare_index_run", failed)
    assert await indexing._index_generation(None, None, None, uuid4(), uuid4()) == 0  # type: ignore[arg-type]

    async def prepared(*args: Any) -> tuple[Any, None]:
        return object(), None

    async def idle(c: Any, state: Any) -> None:
        state.idle = True

    monkeypatch.setattr(indexing, "_prepare_index_run", prepared)
    monkeypatch.setattr(indexing, "_claim_next_chunk", idle)
    assert await indexing._index_generation(None, None, None, uuid4(), uuid4()) is None  # type: ignore[arg-type]


async def test_process_claimed_chunk_maps_outcomes(monkeypatch: pytest.MonkeyPatch) -> None:
    from modules.search import indexing

    state = indexing._RunState()
    ctx = SimpleNamespace(
        factory=None,
        scope=None,
        settings=None,
        original=None,
        generation_id=uuid4(),
        workspace_id=uuid4(),
    )
    claimed = indexing._ClaimedChunk(uuid4(), "c", uuid4(), uuid4(), 3)
    failed: list[str] = []

    async def fail_item(*args: Any, **kwargs: Any) -> None:
        failed.append("failed")

    monkeypatch.setattr(indexing, "_fail_item", fail_item)

    def embedding_raises(exc: BaseException) -> Any:
        async def raiser(*args: Any, **kwargs: Any) -> Any:
            raise exc

        return raiser

    monkeypatch.setattr(
        indexing, "_embed_claimed_chunk", embedding_raises(indexing._AuthorityChanged())
    )
    assert await indexing._process_claimed_chunk(ctx, state, claimed) is False  # type: ignore[arg-type]
    monkeypatch.setattr(
        indexing, "_embed_claimed_chunk", embedding_raises(ConflictError(detail="x"))
    )
    assert await indexing._process_claimed_chunk(ctx, state, claimed) is False  # type: ignore[arg-type]
    monkeypatch.setattr(indexing, "_embed_claimed_chunk", embedding_raises(ValueError("dims")))
    assert await indexing._process_claimed_chunk(ctx, state, claimed) is True  # type: ignore[arg-type]
    assert failed == ["failed"]

    async def nothing(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(indexing, "_embed_claimed_chunk", nothing)
    assert await indexing._process_claimed_chunk(ctx, state, claimed) is True  # type: ignore[arg-type]
    assert hashlib.sha256(
        b""
    ).hexdigest()  # keep hashlib import exercised for the digest tests above


# --- search: cursor wire compatibility with the shared pagination helpers -----------------------------


def test_search_cursor_matches_pre_extraction_encoding() -> None:
    import base64
    import json

    from modules.search import public as search_public
    from modules.search.schemas import SearchRequest

    request = SearchRequest(query="alpha", mode="hybrid", limit=5)
    content = request.model_dump(exclude={"cursor", "limit"}, mode="json")
    content["destination"] = "local"
    content["source_generations"] = []
    legacy_scope = hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()[:16]
    legacy = base64.urlsafe_b64encode(f"{legacy_scope}:25".encode()).decode().rstrip("=")
    assert search_public._encode_cursor(request, 25) == legacy
    assert search_public._offset(request.model_copy(update={"cursor": legacy})) == 25
