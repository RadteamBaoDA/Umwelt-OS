"""P15-T6a: topic/source rule conditions, read-only preview, usage lookup and validation."""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from modules.dashboard import public
from modules.dashboard.highlights import evaluate_highlights
from modules.dashboard.schemas import (
    GadgetDefinitionCreate,
    HighlightPreviewRequest,
    HighlightRule,
)

SRC_A, SRC_B, TOPIC = uuid4(), uuid4(), uuid4()


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


def test_legacy_rules_unchanged_and_fingerprint_dump_stable() -> None:
    r = HighlightRule.model_validate(
        {"id": str(uuid4()), "keywords": ["a"], "severity": "info", "notify": False}
    )
    assert set(r.model_dump(mode="json", exclude_defaults=True)) == {"id", "keywords", "severity", "notify"}
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
    with pytest.raises(ValueError, match="topic"):
        await public.validate_highlight_rules(
            None, 1, [SRC_A], [rule(topic_ids=[uuid4()])]  # type: ignore[arg-type]
        )


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

    async def spy(*args: Any, **kwargs: Any) -> None:
        calls.append((args, kwargs))

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
async def test_preview_rejects_foreign_topic_and_widened_sources(
    monkeypatch: pytest.MonkeyPatch, news: FakeNews,
) -> None:
    patch_projections(monkeypatch, [([], None)], [])
    bad_topic = HighlightPreviewRequest(source_ids=[SRC_A], rules=[rule(topic_ids=[uuid4()])])
    with pytest.raises(ValueError, match="topic"):
        await public.preview_highlights(ReadOnlySession(), 1, bad_topic)  # type: ignore[arg-type]
    widened = HighlightPreviewRequest(source_ids=[SRC_A], rules=[rule(keywords=["a"], source_ids=[SRC_B])])
    with pytest.raises(ValueError, match="subset"):
        await public.preview_highlights(ReadOnlySession(), 1, widened)  # type: ignore[arg-type]


# --- usage lookup ----------------------------------------------------------------------------


class UsageSession:
    def __init__(self, exists: bool, rows: list[tuple[Any, ...]]) -> None:
        self.exists, self.rows = exists, rows

    async def scalar(self, _stmt: Any) -> Any:
        return uuid4() if self.exists else None

    async def execute(self, _stmt: Any) -> Any:
        return SimpleNamespace(all=lambda: self.rows)


@pytest.mark.asyncio
async def test_usage_lookup() -> None:
    d = uuid4()
    rows = [(d, "Overview", 2)]
    (usage,) = await public.definition_usage(UsageSession(True, rows), 1, uuid4()) or []  # type: ignore[arg-type]
    assert (usage.dashboard_id, usage.name, usage.instance_count) == (d, "Overview", 2)
    assert await public.definition_usage(UsageSession(False, []), 1, uuid4()) is None  # type: ignore[arg-type]
