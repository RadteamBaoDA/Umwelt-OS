"""Run-page evidence batching and approval link batching keep per-row visibility."""

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from modules.agents import public


def _row(fenced: bool) -> Any:
    return SimpleNamespace(source_fences={"x": 1} if fenced else None, answer="a", id=uuid4())


@pytest.mark.asyncio
async def test_read_current_results_hides_only_stale_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [_row(False), _row(True), _row(True), _row(True)]
    results = {r.id: SimpleNamespace(answer="a", model_copy=lambda update: SimpleNamespace(answer=update["answer"]))
               for r in rows}
    monkeypatch.setattr(public, "_read", lambda r: results[r.id])
    monkeypatch.setattr(public, "_result_principal", lambda r: None if r is rows[3] else object())
    monkeypatch.setattr(public, "_restore_fences", lambda v: {"records": [], "source_generations": {}})
    seen: list[int] = []

    async def many(_f: Any, items: list[Any], **_k: Any) -> list[bool]:
        seen.append(len(items))
        return [True, False]

    monkeypatch.setattr(public, "revalidate_native_output_fences_many", many)
    out = await public._read_current_results(rows, object(), multi_workspace_enabled=False)  # type: ignore[arg-type]
    assert [o.answer for o in out] == ["a", "a", None, None]
    assert seen == [2]  # one batched call for the two rows with usable principals


@pytest.mark.asyncio
async def test_read_current_results_without_factory_suppresses_fenced_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [_row(False), _row(True)]
    results = {r.id: SimpleNamespace(answer="a", model_copy=lambda update: SimpleNamespace(answer=update["answer"]))
               for r in rows}
    monkeypatch.setattr(public, "_read", lambda r: results[r.id])
    out = await public._read_current_results(rows, None, multi_workspace_enabled=False)
    assert [o.answer for o in out] == ["a", None]


@pytest.mark.asyncio
async def test_conversation_approvals_batch_session_and_link_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import UTC, datetime

    from core.auth import public as auth
    from modules.chat import public as chat

    run_a, run_b, run_dead = uuid4(), uuid4(), uuid4()
    conv = uuid4()

    def approval(run_id: Any) -> Any:
        return SimpleNamespace(
            id=uuid4(), action_id=uuid4(), run_id=run_id, tool_name="t", tool_version="1", arguments={"a": 1},
            argument_hash="h", destination_id="d", destination_revision="1", status="pending",
            created_at=datetime.now(UTC), expires_at=datetime.now(UTC), auth_session_hash="hash",
        )

    rows = [approval(run_a), approval(run_a), approval(run_b), approval(run_dead)]
    counts = {"session": 0, "live": 0, "revoked": 0}

    async def admit(*_a: Any, **_k: Any) -> None:
        return None

    async def revalidate(*_a: Any, **_k: Any) -> bool:
        counts["session"] += 1
        return True

    async def run_ids(*_a: Any, **_k: Any) -> list[Any]:
        return [run_a, run_b, run_dead]

    async def live(_s: Any, ids: list[Any], *_a: Any, **_k: Any) -> frozenset[Any]:
        counts["live"] += 1
        assert len(ids) == len(set(ids))
        return frozenset({run_a, run_b})

    async def revoked(*_a: Any, **_k: Any) -> bool:
        counts["revoked"] += 1
        return False

    async def fences(*_a: Any, **_k: Any) -> bool:
        return True

    class _Session:
        async def scalars(self, *_a: Any) -> Any:
            return SimpleNamespace(all=lambda: rows)

        async def execute(self, *_a: Any) -> Any:
            return SimpleNamespace(all=list)

    monkeypatch.setattr(public, "admit", admit)
    monkeypatch.setattr(public, "actor", lambda _s: 1)
    monkeypatch.setattr(auth, "revalidate_account_session", revalidate)
    monkeypatch.setattr(chat, "list_agent_run_ids_for_owner", run_ids)
    monkeypatch.setattr(chat, "filter_live_agent_run_ids", live)
    monkeypatch.setattr(public, "_approval_run_is_revoked", revoked)
    monkeypatch.setattr(public, "_approval_fences_current", fences)
    out = await public.list_conversation_approvals(
        _Session(), object(), conv, "hash",  # type: ignore[arg-type]
        scope=SimpleNamespace(workspace_id=uuid4()), multi_workspace_enabled=False,  # type: ignore[arg-type]
    )
    assert len(out) == 3  # the approval whose run has no live link is hidden
    assert counts == {"session": 1, "live": 1, "revoked": 2}  # revocation cached per run
