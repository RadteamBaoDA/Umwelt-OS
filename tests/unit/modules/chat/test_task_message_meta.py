"""Scheduled-task metadata exposed on MessageRead: allowlist, origin gate and malformed-data tolerance."""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from modules.chat._routes.mutation import _message_reads


def _read(metadata: object) -> object:
    m = SimpleNamespace(
        id=uuid4(),
        conversation_id=uuid4(),
        role="assistant",
        content="hi",
        client_request_id=None,
        model_identity=None,
        response_id=None,
        revision_of_message_id=None,
        created_at=datetime.now(UTC),
        metadata_json=metadata,
    )
    return _message_reads([m], [[]], {}, {})[0]  # type: ignore[arg-type]


_GOOD = {
    "origin": "scheduled_task",
    "task_kind": "daily_brief",
    "run_id": "r1",
    "brief_id": "b1",
    "brief_revision": 2,
    "brief_citations": [{"ref": 1, "kind": "story", "id": "s1", "title": "T", "source_ids": ["a"]}],
    "secret": "private",
}


def test_allowlisted_keys_only() -> None:
    dumped = _read(_GOOD).model_dump(mode="json")["task"]  # type: ignore[attr-defined]
    assert set(dumped) == {"task_kind", "run_id", "brief_id", "brief_revision", "brief_citations"}
    assert dumped["brief_citations"][0]["title"] == "T"


def test_non_task_and_malformed_metadata_give_none() -> None:
    assert _read({"task_kind": "daily_brief"}).task is None  # type: ignore[attr-defined]
    assert _read({"origin": "scheduled_task", "task_kind": "daily_brief"}).task is None  # type: ignore[attr-defined]
    assert _read({**_GOOD, "brief_citations": "bad"}).task is None  # type: ignore[attr-defined]
    assert _read(None).task is None  # type: ignore[attr-defined]


_TOPIC = {
    "origin": "scheduled_task",
    "task_kind": "topic_watch",
    "run_id": "r1",
    "source_id": "s1",
    "coverage": "partial",
    "stats": {"queries": 2, "items": 1, "reason": "budget", "secret": "x"},
    "items": [
        {
            "title": "T",
            "url": "https://example.com/a",
            "site": "example.com",
            "summary": "S",
            "document_id": "d1",
            "private": "x",
        },
        {"title": "Bad url", "url": "javascript:alert(1)"},
    ],
    "secret": "private",
}


def test_topic_watch_meta_is_allowlisted() -> None:
    task = _read(_TOPIC).task  # type: ignore[attr-defined]
    dumped = task.model_dump(mode="json")
    assert set(dumped) == {"task_kind", "run_id", "coverage", "stats", "items"}
    assert set(dumped["items"][0]) == {"title", "url", "site", "summary", "document_id", "story_id"}
    assert "secret" not in dumped["stats"] and dumped["stats"]["reason"] == "budget"
    assert dumped["items"][1]["url"] is None  # non-http(s) links are dropped, never rendered


def test_topic_watch_meta_malformed_gives_none() -> None:
    assert _read({**_TOPIC, "items": "bad"}).task is None  # type: ignore[attr-defined]
    assert _read({**_TOPIC, "coverage": "weird"}).task is None  # type: ignore[attr-defined]
    assert _read({**_TOPIC, "task_kind": "other"}).task is None  # type: ignore[attr-defined]
