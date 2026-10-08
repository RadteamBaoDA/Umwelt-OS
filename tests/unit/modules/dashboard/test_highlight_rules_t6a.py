"""P15-T6a: topic/source rule conditions, read-only preview, usage lookup and validation."""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

from modules.dashboard import public
from modules.dashboard.highlights import evaluate_highlights
from modules.dashboard.schemas import (
    GadgetDefinitionCreate,
    GadgetDefinitionPatch,
    HighlightPreviewRequest,
    HighlightRule,
)

SRC_A, SRC_B, TOPIC = uuid4(), uuid4(), uuid4()


def sql_text(stmt: Any) -> str:
    """Render a statement with literal values so tests can assert its filters."""
    return str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


def rule(**kw: Any) -> HighlightRule:
    base: dict[str, Any] = {"id": uuid4(), "severity": "warning", "notify": True, "keywords": []}
    return HighlightRule(**{**base, **kw})


# --- condition evaluation -------------------------------------------------------------------


def test_topic_terms_match_keywords_and_entity_names() -> None:
    r = rule(topic_ids=[TOPIC])
    hits = evaluate_highlights("Acme Corp raises rates", [r], topic_terms={TOPIC: ["rates", "Acme Corp"]})
    assert hits and set(hits[0].matched_keywords) == {"rates", "Acme Corp"}


def test_unresolved_topic_never_matches_and_is_reported() -> None:
    only_topic = rule(topic_ids=[TOPIC])
    assert evaluate_highlights("rates", [only_topic], topic_terms={}) == []
    mixed = rule(topic_ids=[TOPIC], keywords=["rates"])
    (hit,) = evaluate_highlights("rates", [mixed], topic_terms={})
    assert "1 topic(s) unavailable" in hit.reason


def test_source_include_and_exclude() -> None:
    inc = rule(keywords=["x"], source_ids=[SRC_A])
    exc = rule(keywords=["x"], exclude_source_ids=[SRC_A])
    assert evaluate_highlights("x", [inc], source_id=SRC_A)
    assert not evaluate_highlights("x", [inc], source_id=SRC_B)
    assert not evaluate_highlights("x", [exc], source_id=SRC_A)
    assert evaluate_highlights("x", [exc], source_id=SRC_B)


def test_legacy_rules_unchanged_match_reason() -> None:
    r = HighlightRule.model_validate(
        {"id": str(uuid4()), "keywords": ["a"], "severity": "info", "notify": False}
    )
    assert evaluate_highlights("a b", [r])[0].reason == "Matched 1 keyword(s): a"


# --- validation -----------------------------------------------------------------------------


def test_schema_validation_bounds() -> None:
    with pytest.raises(ValidationError):
        rule()  # neither keywords nor topics
    with pytest.raises(ValidationError):
        rule(topic_ids=[uuid4() for _ in range(9)])
    with pytest.raises(ValidationError):
        rule(keywords=["a"], source_ids=[SRC_A], exclude_source_ids=[SRC_A])
    with pytest.raises(ValidationError):
        rule(keywords=["a"], source_ids=[SRC_A, SRC_A])
    with pytest.raises(ValidationError):
        rule(keywords=["a"], unknown_field=1)
    with pytest.raises(ValidationError):
        HighlightPreviewRequest(source_ids=[SRC_A], rules=[rule(keywords=["a"])], days=8)
    assert GadgetDefinitionCreate(name="n", renderer="highlights", highlight_rules=[rule(topic_ids=[TOPIC])])


class FakeNews:
    def __init__(self, live: set[Any], terms: dict[Any, list[str]]) -> None:
        self.live, self.terms = live, terms

    async def live_topic_ids(self, _s: Any, _o: int, ids: list[Any]) -> set[Any]:
        return {i for i in ids if i in self.live}

    async def resolve_topic_terms(self, _s: Any, _o: int, ids: list[Any]) -> dict[Any, list[str]]:
        return {i: self.terms[i] for i in ids if i in self.terms}


@pytest.fixture
def news(monkeypatch: pytest.MonkeyPatch) -> FakeNews:
    fake = FakeNews({TOPIC}, {TOPIC: ["rates"]})
    import modules.news.public as news_public

    monkeypatch.setattr(news_public, "live_topic_ids", fake.live_topic_ids)
    monkeypatch.setattr(news_public, "resolve_topic_terms", fake.resolve_topic_terms)
    return fake


@pytest.mark.asyncio
async def test_validate_rules_subset_and_topic_ownership(news: FakeNews) -> None:
    ok = rule(topic_ids=[TOPIC], source_ids=[SRC_A])
    await public.validate_highlight_rules(None, 1, [SRC_A], [ok])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="subset"):
        await public.validate_highlight_rules(None, 1, [SRC_B], [ok])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="subset"):
        await public.validate_highlight_rules(
            None, 1, [SRC_B], [rule(keywords=["a"], exclude_source_ids=[SRC_A])]  # type: ignore[arg-type]
        )
    with pytest.raises(public.HighlightRuleError) as err:
        await public.validate_highlight_rules(
            None, 1, [SRC_A], [rule(topic_ids=[uuid4()])]  # type: ignore[arg-type]
        )
    assert err.value.code == "rule_topic_unknown"


@pytest.mark.asyncio
async def test_validate_rejects_rule_excluding_every_source(news: FakeNews) -> None:
    every = rule(keywords=["a"], exclude_source_ids=[SRC_A, SRC_B])
    with pytest.raises(public.HighlightRuleError) as err:
        await public.validate_highlight_rules(None, 1, [SRC_A, SRC_B], [every])  # type: ignore[arg-type]
    assert err.value.code == "rule_excludes_all_sources"
    some = rule(keywords=["a"], exclude_source_ids=[SRC_A])
    await public.validate_highlight_rules(None, 1, [SRC_A, SRC_B], [some])  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_validate_only_checks_newly_added_topics(news: FakeNews) -> None:
    dead = uuid4()  # stored earlier, deleted since
    stale = rule(topic_ids=[dead, TOPIC])
    await public.validate_highlight_rules(None, 1, [SRC_A], [stale], known_topic_ids={dead})  # type: ignore[arg-type]
    with pytest.raises(public.HighlightRuleError):
        await public.validate_highlight_rules(None, 1, [SRC_A], [stale])  # type: ignore[arg-type]
    with pytest.raises(public.HighlightRuleError):  # a new dead id is still rejected
        await public.validate_highlight_rules(
            None, 1, [SRC_A], [rule(topic_ids=[dead, uuid4()])], known_topic_ids={dead},  # type: ignore[arg-type]
        )


class _Stop(Exception):
    """Raised by the validation spy to end create/patch right after validation."""


class _PatchSession:
    async def scalars(self, _stmt: Any) -> Any:
        return SimpleNamespace(all=list)


@pytest.mark.asyncio
async def test_create_and_patch_call_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any]] = []

    async def fake_validate(_s: Any, _o: int, source_ids: Any, rules: Any, **kw: Any) -> None:
        seen.append({"sources": list(source_ids), "rules": list(rules), **kw})
        raise _Stop

    async def fake_lock_sources(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(public, "validate_highlight_rules", fake_validate)
    monkeypatch.setattr(public, "_lock_selected_sources", fake_lock_sources)
    monkeypatch.setattr(public, "_lock_owner_creation_quota", fake_lock_sources)
    r = rule(topic_ids=[TOPIC])
    payload = GadgetDefinitionCreate(name="n", renderer="highlights", source_ids=[SRC_A], highlight_rules=[r])
    with pytest.raises(_Stop):
        await public.create_definition(object(), 1, payload)  # type: ignore[arg-type]
    assert seen[0]["sources"] == [SRC_A] and "known_topic_ids" not in seen[0]  # create: every topic is checked

    row = SimpleNamespace(
        revision=1, name="n", renderer="highlights", source_ids=[str(SRC_A)], scope={}, filters={},
        highlight_rules=[r.model_dump(mode="json")],
    )

    async def lock_definition(*_a: Any, **_k: Any) -> Any:
        return row

    monkeypatch.setattr(public, "_lock_definition", lock_definition)
    with pytest.raises(_Stop):
        await public.patch_definition(
            _PatchSession(), 1, uuid4(), GadgetDefinitionPatch(expected_revision=1, name="renamed"),  # type: ignore[arg-type]
        )
    assert seen[1]["known_topic_ids"] == {TOPIC}  # stored topics are not re-checked on a rename


# --- preview: read-only, bounded, privacy ----------------------------------------------------


class ReadOnlySession:
    """Any write-ish call fails the test; reads are not used because projections are faked."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"preview touched session.{name}")


def item(source: Any, excerpt: str) -> SimpleNamespace:
    return SimpleNamespace(
        document_id=uuid4(), document_version_id=uuid4(), source_id=source, title="t",
        observed_at=datetime.now(UTC), excerpt=excerpt,
    )


@pytest.fixture
def emit_spy(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    calls: list[Any] = []
    import modules.notifications.public as notif

    async def spy(*args: Any, **kwargs: Any) -> bool:
        calls.append((args, kwargs))
        return True

    monkeypatch.setattr(notif, "emit", spy)
    return calls


def patch_projections(monkeypatch: pytest.MonkeyPatch, pages: list[tuple[list[Any], str | None]], seen: list[Any]) -> None:
    from modules.knowledge.documents import public as documents

    queue = list(pages)

    async def fake(_s: Any, **kw: Any) -> SimpleNamespace:
        seen.append(kw)
        items, cursor = queue.pop(0)
        return SimpleNamespace(items=items, next_cursor=cursor)

    monkeypatch.setattr(documents, "list_gadget_document_projections", fake)


@pytest.mark.asyncio
async def test_preview_is_read_only_never_notifies_and_bounded(
    monkeypatch: pytest.MonkeyPatch, news: FakeNews, emit_spy: list[Any],
) -> None:
    seen: list[Any] = []
    patch_projections(
        monkeypatch,
        [([item(SRC_A, "rates up"), item(SRC_B, "rates up")], "c1"), ([item(SRC_A, "nothing")], "c2")],
        seen,
    )
    gone = uuid4()
    news.live.add(gone)  # live but inactive: validates, yet resolves to no terms
    r = rule(topic_ids=[TOPIC, gone], source_ids=[SRC_A], notify=True)
    payload = HighlightPreviewRequest(source_ids=[SRC_A, SRC_B], rules=[r], days=3)
    result = await public.preview_highlights(ReadOnlySession(), 1, payload)  # type: ignore[arg-type]
    assert emit_spy == []  # no notification call
    assert result.scanned == 3 and result.truncated is True  # 2 pages max, cursor left over
    assert len(seen) == 2 and all(kw["limit"] <= 100 for kw in seen)
    assert (datetime.now(UTC) - seen[0]["since"]).days == 3
    assert result.rules[0].match_count == 1 and result.rules[0].unresolved_topic_ids == [gone]
    assert [m.source_id for m in result.matches] == [SRC_A]


@pytest.mark.asyncio
async def test_preview_not_truncated_when_cursor_exhausted(
    monkeypatch: pytest.MonkeyPatch, news: FakeNews, emit_spy: list[Any],
) -> None:
    patch_projections(monkeypatch, [([item(SRC_A, "rates")], None)], [])
    payload = HighlightPreviewRequest(source_ids=[SRC_A], rules=[rule(keywords=["rates"])])
    result = await public.preview_highlights(ReadOnlySession(), 1, payload)  # type: ignore[arg-type]
    assert result.truncated is False and result.window_days == 7 and emit_spy == []


@pytest.mark.asyncio
async def test_preview_reports_dead_topic_unresolved_and_rejects_widened_sources(
    monkeypatch: pytest.MonkeyPatch, news: FakeNews,
) -> None:
    patch_projections(monkeypatch, [([item(SRC_A, "rates")], None)], [])
    dead = uuid4()
    request = HighlightPreviewRequest(source_ids=[SRC_A], rules=[rule(topic_ids=[dead])])
    result = await public.preview_highlights(ReadOnlySession(), 1, request)  # type: ignore[arg-type]
    assert result.rules[0].unresolved_topic_ids == [dead] and result.rules[0].match_count == 0
    widened = HighlightPreviewRequest(source_ids=[SRC_A], rules=[rule(keywords=["a"], source_ids=[SRC_B])])
    with pytest.raises(public.HighlightRuleError, match="subset"):
        await public.preview_highlights(ReadOnlySession(), 1, widened)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_preview_respects_item_scope(monkeypatch: pytest.MonkeyPatch, news: FakeNews) -> None:
    inside, outside = item(SRC_A, "rates"), item(SRC_A, "rates")
    patch_projections(monkeypatch, [([inside, outside], None)], [])
    request = HighlightPreviewRequest(
        source_ids=[SRC_A], rules=[rule(keywords=["rates"])], source_item_ids=[inside.document_id],
    )
    result = await public.preview_highlights(ReadOnlySession(), 1, request)  # type: ignore[arg-type]
    assert result.scanned == 1 and result.rules[0].match_count == 1


# --- usage lookup ----------------------------------------------------------------------------


class UsageSession:
    def __init__(self, exists: bool, rows: list[tuple[Any, ...]]) -> None:
        self.exists, self.rows = exists, rows

    async def scalar(self, _stmt: Any) -> Any:
        return uuid4() if self.exists else None

    async def execute(self, _stmt: Any) -> Any:
        return SimpleNamespace(all=lambda: self.rows)


@pytest.mark.asyncio
async def test_usage_query_is_owner_scoped() -> None:
    captured: list[Any] = []

    class Capture(UsageSession):
        async def scalar(self, stmt: Any) -> Any:
            captured.append(stmt)
            return uuid4()

        async def execute(self, stmt: Any) -> Any:
            captured.append(stmt)
            return SimpleNamespace(all=list)

    await public.definition_usage(Capture(True, []), 7, uuid4())  # type: ignore[arg-type]
    sql = [sql_text(stmt) for stmt in captured]
    assert len(sql) == 2
    assert "gadget_definitions.owner_id = 7" in sql[0]
    assert "dashboards.owner_id = 7" in sql[1]


@pytest.mark.asyncio
async def test_usage_lookup() -> None:
    d = uuid4()
    rows = [(d, "Overview", 2)]
    (usage,) = await public.definition_usage(UsageSession(True, rows), 1, uuid4()) or []  # type: ignore[arg-type]
    assert (usage.dashboard_id, usage.name, usage.instance_count) == (d, "Overview", 2)
    assert await public.definition_usage(UsageSession(False, []), 1, uuid4()) is None  # type: ignore[arg-type]


# --- golden fingerprint + scheduled notification path -----------------------------------------

RULE_ID = UUID("00000000-0000-0000-0000-0000000000a1")
FP_SOURCE = UUID("00000000-0000-0000-0000-0000000000b1")
# sha256 of the pre-T6a payload (legacy 4-key rule). Changing it re-scans every stored cursor.
LEGACY_FINGERPRINT = "4c8797dc1cb8669a12d8e66f7516ccbb5a5dc37a62d07c1edfea5eb77ade8d13"


class EmitSession:
    """Session double for the locked scheduled path: records progress and commits."""

    def __init__(self, definition: Any) -> None:
        self.definition, self.progress, self.commits = definition, None, 0
        self.suppressed: set[str] = set()

    async def execute(self, stmt: Any) -> Any:
        from sqlalchemy import Delete, Insert
        from sqlalchemy.dialects import postgresql

        if isinstance(stmt, Delete):
            self.suppressed.clear()
        elif isinstance(stmt, Insert):
            self.suppressed.add(stmt.compile(dialect=postgresql.dialect()).params["dedupe_key"])  # type: ignore[no-untyped-call]
        return SimpleNamespace(scalars=lambda: list(self.suppressed))

    async def scalar(self, _stmt: Any) -> Any:
        return self.definition

    async def get(self, _model: Any, _key: Any, **_kw: Any) -> Any:
        return self.progress

    def add(self, row: Any) -> None:
        self.progress = row

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1


def run_emit(monkeypatch: pytest.MonkeyPatch, definition: Any, items: list[Any]) -> EmitSession:
    from modules.knowledge.documents import public as documents

    async def page(_s: Any, **_kw: Any) -> SimpleNamespace:
        return SimpleNamespace(
            items=items, selection_fences=[], has_more=False, cursor_created_at=None, cursor_version_id=None,
        )

    monkeypatch.setattr(documents, "list_gadget_highlight_projection_page", page)
    return EmitSession(definition)


def definition_row(rules: list[dict[str, Any]], sources: list[UUID]) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(), renderer="highlights", source_ids=[str(s) for s in sources], scope={},
        highlight_rules=rules, revision=1,
    )


@pytest.mark.asyncio
async def test_emit_path_fingerprint_matches_legacy_golden(
    monkeypatch: pytest.MonkeyPatch, news: FakeNews, emit_spy: list[Any],
) -> None:
    legacy = {"id": str(RULE_ID), "keywords": ["rates"], "severity": "warning", "notify": True}
    session = run_emit(monkeypatch, definition_row([legacy], [FP_SOURCE]), [])
    await public.evaluate_gadget_highlights(session, 1, uuid4(), emit_notifications=True)  # type: ignore[arg-type]
    assert session.progress.rules_fingerprint == LEGACY_FINGERPRINT
    # Re-stored in the new shape (explicit empty lists) must not change it either.
    restored = {**legacy, "topic_ids": [], "source_ids": [], "exclude_source_ids": []}
    session = run_emit(monkeypatch, definition_row([restored], [FP_SOURCE]), [])
    await public.evaluate_gadget_highlights(session, 1, uuid4(), emit_notifications=True)  # type: ignore[arg-type]
    assert session.progress.rules_fingerprint == LEGACY_FINGERPRINT


@pytest.mark.asyncio
async def test_emit_path_applies_source_and_topic_conditions(
    monkeypatch: pytest.MonkeyPatch, news: FakeNews, emit_spy: list[Any],
) -> None:
    excluded = rule(topic_ids=[TOPIC], exclude_source_ids=[SRC_B])
    items = [item(SRC_A, "Rates rise"), item(SRC_B, "Rates rise"), item(SRC_A, "weather")]
    session = run_emit(monkeypatch, definition_row([excluded.model_dump(mode="json")], [SRC_A, SRC_B]), items)
    matches = await public.evaluate_gadget_highlights(  # type: ignore[arg-type]
        session, 1, uuid4(), emit_notifications=True,
    )
    assert [m.source_id for m in matches] == [SRC_A]  # excluded source and non-topic text both skipped
    assert len(emit_spy) == 1 and session.commits == 1
    assert emit_spy[0][0][2].body.startswith("Matched 1 keyword(s): rates")


def test_term_cap_per_rule() -> None:
    from modules.dashboard.highlights import MAX_TERMS_PER_RULE, compile_rules

    terms = {TOPIC: [f"t{i}" for i in range(MAX_TERMS_PER_RULE + 50)]}
    (compiled,) = compile_rules([rule(topic_ids=[TOPIC])], terms)
    assert len(compiled.patterns) == MAX_TERMS_PER_RULE
