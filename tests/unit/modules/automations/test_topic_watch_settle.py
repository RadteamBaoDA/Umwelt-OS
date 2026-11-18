"""Settle sweep: one thread post per finished Topic watch run, idempotent on retry."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from core.errors import NotFoundError
from core.workspaces.schemas import InternalJobScope
from modules.automations import _topic_settle as settle

SCOPE = InternalJobScope(workspace_id=uuid4(), actor_user_id=1, membership_revision=1)
NOW = datetime.now(UTC)


def _cand(**over: Any) -> settle.Candidate:
    base: dict[str, Any] = {
        "run_id": uuid4(),
        "ordinal": 1,
        "automation_id": uuid4(),
        "rule_name": "AI chips",
        "since": NOW,
        "request_id": uuid4(),
        "source_id": uuid4(),
        "max_items": 5,
    }
    return settle.Candidate(**{**base, **over})


def _doc(i: int, **over: Any) -> Any:
    base = {
        "id": uuid4(),
        "title": f"Title {i}",
        "canonical_url": f"https://example.com/{i}",
        "site": "example.com",
        "summary": f"Summary {i}",
        "created_at": NOW,
    }
    return SimpleNamespace(**{**base, **over})


class World:
    """Fakes for the owner reads and the two write seams, recording what the sweep did."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.status = "succeeded"
        self.docs: list[Any] = []
        self.stats: dict[str, Any] | None = {"items": 2, "reason": None, "queries": 3}
        self.published: list[Any] = []
        self.closed: list[tuple[Any, str]] = []
        self.raise_missing = False
        self.until: datetime | None = None
        self.in_flight = False
        self.request_error: str | None = None
        self.read_until: Any = "unset"

        async def get_request(
            _s: Any, _scope: Any, source_id: Any, request_id: Any, **_k: Any
        ) -> Any:
            if self.raise_missing:
                raise NotFoundError("gone")
            return SimpleNamespace(
                status=self.status, ingestion_run_id=uuid4(), error_code=self.request_error
            )

        async def recent(
            _s: Any, source_id: Any, since: Any, limit: int, *, until: Any = None, **_k: Any
        ) -> list[Any]:
            assert limit == 5 and since == NOW
            self.read_until = until
            return self.docs

        async def runs(_s: Any, source_id: Any, **_k: Any) -> Any:
            return SimpleNamespace(items=[], current_run=None)

        async def publish(_f: Any, cand: Any, post: Any, **_k: Any) -> bool:
            self.published.append(post)
            return True

        async def close(_f: Any, cand: Any, code: str, **_k: Any) -> None:
            self.closed.append((cand, code))

        monkeypatch.setattr(settle.connectors, "get_collection_request", get_request)
        monkeypatch.setattr(settle.documents, "list_recent_source_documents", recent)
        monkeypatch.setattr(settle.ingestion, "list_source_runs", runs)
        monkeypatch.setattr(settle, "_receipt", self._receipt)
        monkeypatch.setattr(settle, "_publish", publish)
        monkeypatch.setattr(settle, "_close", close)
        monkeypatch.setattr(settle, "_read_session", _FakeSession)

    async def _receipt(
        self, *_a: Any, **_k: Any
    ) -> tuple[dict[str, Any] | None, datetime | None, bool]:
        return self.stats, self.until, self.in_flight


async def _noop() -> None:
    return None


class _FakeSession:
    """Async context manager standing in for ``factory()``."""

    def __init__(self, _factory: Any) -> None:
        pass

    async def __aenter__(self) -> Any:
        return SimpleNamespace(rollback=_noop)

    async def __aexit__(self, *_a: Any) -> None:
        return None


async def _run(cand: settle.Candidate) -> str:
    return await settle.settle_candidate(  # type: ignore[arg-type]
        object(), cand, scope=SCOPE, multi_workspace_enabled=False
    )


@pytest.mark.asyncio
async def test_posts_once_per_run_with_run_dedupe_key(monkeypatch: pytest.MonkeyPatch) -> None:
    w = World(monkeypatch)
    w.docs = [_doc(1), _doc(2)]
    cand = _cand()
    assert await _run(cand) == "posted"
    assert await _run(cand) == "posted"  # a retry rebuilds the same post; chat dedupes on the key
    first, second = w.published
    assert first.dedupe_key == second.dedupe_key == f"run:{cand.run_id}"
    assert first.metadata["task_kind"] == "topic_watch"
    assert first.metadata["run_id"] == str(cand.run_id)
    assert first.metadata["source_id"] == str(cand.source_id)
    assert [i["title"] for i in first.metadata["items"]] == ["Title 1", "Title 2"]
    assert first.metadata["items"][0]["url"] == "https://example.com/1"
    assert first.metadata["coverage"] == "complete"
    assert first.content == "- Title 1: Summary 1\n- Title 2: Summary 2"


@pytest.mark.asyncio
async def test_running_request_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    w = World(monkeypatch)
    w.docs = [_doc(1)]
    for status in ("queued", "running"):
        w.status = status
        assert await _run(_cand()) == "pending"
    assert w.published == [] and w.closed == []


@pytest.mark.asyncio
async def test_zero_items_posts_nothing_and_records_no_new_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = World(monkeypatch)
    w.status = "no_changes"
    assert await _run(_cand()) == "no_new_data"
    assert w.published == [] and [c for _cand_, c in w.closed] == ["no_new_data"]


@pytest.mark.asyncio
async def test_partial_run_carries_coverage_and_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    w = World(monkeypatch)
    w.docs = [_doc(1)]
    w.stats = {"items": 1, "reason": "budget", "tokens": 9, "ignored": "x"}
    await _run(_cand())
    meta = w.published[0].metadata
    assert meta["coverage"] == "partial" and meta["stats"]["reason"] == "budget"
    assert "ignored" not in meta["stats"]


@pytest.mark.asyncio
async def test_failed_or_vanished_request_is_closed_without_a_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = World(monkeypatch)
    w.status = "failed"
    assert await _run(_cand()) == "closed"
    w.raise_missing = True
    assert await _run(_cand()) == "closed"
    assert [c for _cand_, c in w.closed] == ["collection_failed", "collection_unavailable"]
    assert w.published == []


def test_post_text_is_bounded_and_single_line_items() -> None:
    docs = [_doc(1, title="A\nB", summary="S\n\n[1]")]
    post = settle.build_post(_cand(), docs, None)
    assert post.content == "- A B: S [1]"
    assert post.metadata["coverage"] == "complete" and post.metadata["stats"] == {}


@pytest.mark.asyncio
async def test_document_read_is_bounded_by_the_run_end(monkeypatch: pytest.MonkeyPatch) -> None:
    """Documents of a later run must not leak into this run's post."""
    w = World(monkeypatch)
    w.docs = [_doc(1)]
    w.until = NOW
    await _run(_cand())
    assert w.read_until == NOW


def test_post_carries_the_task_id_seen_count_and_inert_titles() -> None:
    cand = _cand()
    docs = [_doc(1, title="Read www.evil.example/x now https://evil.example/y")]
    post = settle.build_post(cand, docs, {"seen": 4, "items": 1})
    assert post.metadata["automation_id"] == str(cand.automation_id)
    assert post.metadata["stats"]["seen"] == 4
    assert "evil" not in post.content and "http" not in post.content
    assert post.metadata["items"][0]["title"].startswith("Read www.evil.example")


@pytest.mark.asyncio
async def test_accepted_request_waits_for_its_ingestion_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Documents appear only when the ingestion run finishes; an in-flight run must not close as no_new_data."""
    w = World(monkeypatch)
    w.in_flight = True
    assert await _run(_cand()) == "pending"
    assert w.closed == [] and w.published == []
    w.in_flight, w.docs = False, [_doc(1)]
    assert await _run(_cand()) == "posted"


@pytest.mark.asyncio
async def test_failed_request_keeps_the_collectors_error_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The owner sees what to fix (e.g. web search not configured), not a generic collection_failed."""
    w = World(monkeypatch)
    w.status, w.request_error = "failed", "web_search_not_configured"
    assert await _run(_cand()) == "closed"
    assert [c for _cand_, c in w.closed] == ["web_search_not_configured"]
