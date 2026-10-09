"""Q2: effective transcript projection for chat generation."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from core.workspaces.schemas import InternalJobScope
from modules.chat import history
from modules.chat.history import Turn, cap_turns, select_turns

T0 = datetime(2026, 1, 1, tzinfo=UTC)
SCOPE = InternalJobScope(workspace_id=uuid4(), actor_user_id=1, membership_revision=1)


def _msg(role: str, content: str, at: datetime, *, revision_of: UUID | None = None, citations: Any = None,
         id: UUID | None = None) -> SimpleNamespace:
    return SimpleNamespace(id=id or uuid4(), role=role, content=content, created_at=at,
                           revision_of_message_id=revision_of, citations=citations or [])


def _turn(i: int, *, status: str = "completed", user_at: datetime | None = None, revision_of: UUID | None = None,
          citations: Any = None, answer: bool = True, text: str | None = None):
    at = user_at or T0 + timedelta(minutes=i)
    user = _msg("user", text or f"q{i}", at, revision_of=revision_of)
    asst = _msg("assistant", text or f"a{i}", at + timedelta(seconds=1), revision_of=revision_of, citations=citations)
    run = SimpleNamespace(id=uuid4(), user_message_id=user.id, status=status, created_at=at,
                          assistant_message_id=asst.id if answer else None)
    return user, asst, run


def _flat(*turns):
    msgs = [m for t in turns for m in t[:2]]
    return msgs, [t[2] for t in turns]


def _texts(turns: list[Turn]) -> list[str]:
    return [t.user.content for t in turns]


def test_latest_twenty_chronological() -> None:
    msgs, runs = _flat(*[_turn(i) for i in range(30)])
    turns = select_turns(list(reversed(msgs)), runs)
    assert _texts(turns) == [f"q{i}" for i in range(10, 30)]


def test_failed_cancelled_pending_and_answerless_dropped() -> None:
    ok = _turn(0)
    bad = [_turn(1, status="failed"), _turn(2, status="cancelled"), _turn(3, status="pending"),
           _turn(4, answer=False)]
    msgs, runs = _flat(ok, *bad)
    assert _texts(select_turns(msgs, runs)) == ["q0"]


def test_edit_supersedes_original_turn() -> None:
    first = _turn(0)
    original = _turn(1)
    edited = _turn(2, revision_of=original[0].id, text="q1-edited")
    msgs, runs = _flat(first, original, edited, _turn(3))
    assert _texts(select_turns(msgs, runs)) == ["q0", "q1-edited", "q3"]


def test_regenerate_supersedes_old_answer_and_its_prompt() -> None:
    original = _turn(1)
    regen = _turn(2, revision_of=original[1].id, text="q1")
    msgs, runs = _flat(original, regen)
    turns = select_turns(msgs, runs)
    assert [t.user.id for t in turns] == [regen[0].id]


def test_failed_revision_does_not_supersede_original() -> None:
    original = _turn(1)
    failed = _turn(2, status="failed", revision_of=original[0].id)
    msgs, runs = _flat(original, failed)
    assert [t.user.id for t in select_turns(msgs, runs)] == [original[0].id]


def test_edit_chain_keeps_only_latest_branch() -> None:
    a = _turn(0)
    b = _turn(1, revision_of=a[0].id, text="v2")
    c = _turn(2, revision_of=b[0].id, text="v3")
    msgs, runs = _flat(a, b, c)
    assert _texts(select_turns(msgs, runs)) == ["v3"]


def test_equal_timestamps_are_deterministic_whatever_input_order() -> None:
    turns = [_turn(i, user_at=T0) for i in range(5)]
    msgs, runs = _flat(*turns)
    expected = select_turns(msgs, runs)
    assert select_turns(list(reversed(msgs)), list(reversed(runs))) == expected
    assert [str(t.user.id) for t in expected] == sorted(str(t[0].id) for t in turns)


def test_char_cap_keeps_newest_whole_turns() -> None:
    msgs, runs = _flat(*[_turn(i, text="x" * 10) for i in range(5)])
    turns = select_turns(msgs, runs)
    assert len(cap_turns(turns, max_chars=45)) == 2  # 20 chars per turn
    assert cap_turns(turns, max_chars=5) == []
    assert cap_turns(turns, max_chars=10_000) == turns


def _cite(chunk: UUID, version: UUID, source: UUID, doc: UUID) -> dict[str, Any]:
    return {"sourceType": "document", "sourceId": str(source), "documentId": str(doc),
            "documentVersionId": str(version), "chunkId": str(chunk), "title": "T", "quote": "Q"}


async def test_revoked_evidence_turn_is_omitted(monkeypatch: pytest.MonkeyPatch) -> None:
    src, doc, ver, chunk = uuid4(), uuid4(), uuid4(), uuid4()
    gone = _turn(0, citations=[_cite(uuid4(), uuid4(), src, doc)])
    live = _turn(1, citations=[_cite(chunk, ver, src, doc)])
    local = _turn(2, citations=[_cite(chunk, ver, src, doc)])
    plain = _turn(3)
    evidence = SimpleNamespace(document_version_id=ver, chunk_id=chunk, source_id=src, document_id=doc,
                               local_only=False)
    lock = AsyncMock(return_value=[evidence])
    monkeypatch.setattr("modules.knowledge.documents.public.lock_chat_evidence_chunks", lock)
    turns = [Turn(t[0], t[1]) for t in (gone, live, plain)]
    assert await history.revoked_assistant_ids(None, turns, SCOPE) == {gone[1].id}  # type: ignore[arg-type]
    assert lock.await_args.kwargs["scope"] == SCOPE
    evidence.local_only = True
    assert await history.revoked_assistant_ids(None, [Turn(local[0], local[1])], SCOPE) == {local[1].id}  # type: ignore[arg-type]


async def test_unparseable_citation_or_lock_failure_is_conservative(monkeypatch: pytest.MonkeyPatch) -> None:
    bad = _turn(0, citations=[{"nonsense": 1}])
    ok = _turn(1, citations=[_cite(uuid4(), uuid4(), uuid4(), uuid4())])
    monkeypatch.setattr("modules.knowledge.documents.public.lock_chat_evidence_chunks",
                        AsyncMock(side_effect=ValueError("changed")))
    got = await history.revoked_assistant_ids(None, [Turn(bad[0], bad[1]), Turn(ok[0], ok[1])], SCOPE)  # type: ignore[arg-type]
    assert got == {bad[1].id, ok[1].id}


async def test_loader_scopes_queries_and_formats(monkeypatch: pytest.MonkeyPatch) -> None:
    turns = [_turn(i) for i in range(3)]
    msgs, runs = _flat(*turns)
    stmts: list[str] = []

    async def scalars(stmt: Any) -> Any:
        stmts.append(str(stmt.compile(compile_kwargs={"literal_binds": False})))
        return SimpleNamespace(all=lambda: msgs if len(stmts) == 1 else runs)

    session = SimpleNamespace(scalars=scalars)
    out = await history.load_effective_history(session, uuid4(), T0 + timedelta(days=1), SCOPE)  # type: ignore[arg-type]
    assert [m["content"] for m in out] == ["q0", "a0", "q1", "a1", "q2", "a2"]
    assert [m["role"] for m in out[:2]] == ["user", "assistant"]
    assert "workspace_id" in stmts[0] and "workspace_id" in stmts[1]
