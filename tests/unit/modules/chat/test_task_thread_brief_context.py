"""Task-thread follow-ups carry the latest live brief as text plus document evidence refs."""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from modules.chat._public import scheduled_task_threads as threads
from modules.chat._worker.setup import _compose_system_text, _load_turn_inputs
from modules.chat._worker.state import _TurnInputs
from modules.chat.schemas import AnswerContextRequest, SelectedEvidenceRef

IN_SCOPE, OUT_SCOPE = uuid4(), uuid4()
BRIEF_ID = uuid4()
META = {"origin": "scheduled_task", "brief_id": str(BRIEF_ID)}
CUTOFF = datetime(2026, 1, 1, tzinfo=UTC)


def _ref(source_id: Any) -> SimpleNamespace:
    return SimpleNamespace(
        fact_ref=1,
        fact_kind="stories",
        source_id=source_id,
        document_id=uuid4(),
        document_version_id=uuid4(),
        chunk_id=uuid4(),
    )


def _session(row: Any) -> MagicMock:
    session = MagicMock()
    session.execute = AsyncMock(return_value=MagicMock(first=MagicMock(return_value=row)))
    return session


def _patch_dashboard(monkeypatch: pytest.MonkeyPatch, *, withheld: bool, refs: list[Any]) -> None:
    from modules.dashboard import public as dashboard

    async def withheld_ids(*_a: Any, **_k: Any) -> frozenset[str]:
        return frozenset({str(BRIEF_ID)}) if withheld else frozenset()

    monkeypatch.setattr(threads, "withheld_brief_ids", withheld_ids)
    monkeypatch.setattr(dashboard, "brief_document_refs", AsyncMock(return_value=refs))


async def _latest(session: Any, scope_ids: list[str]) -> Any:
    return await threads.latest_task_brief(
        session,
        uuid4(),
        datetime.now(UTC),
        scope_ids,
        scope=SimpleNamespace(workspace_id=uuid4(), actor_id=uuid4()),  # type: ignore[arg-type]
        multi_workspace_enabled=False,
    )


@pytest.mark.asyncio
async def test_latest_brief_refs_filtered_to_pinned_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    keep = _ref(IN_SCOPE)
    _patch_dashboard(monkeypatch, withheld=False, refs=[keep, _ref(OUT_SCOPE), keep])
    got = await _latest(_session(("Top story [1] and [2] here", META)), [str(IN_SCOPE)])
    assert got is not None
    assert got.text == "Top story  and  here"
    assert [r.chunk_id for r in got.refs] == [keep.chunk_id]


@pytest.mark.asyncio
async def test_brief_refs_capped_at_brief_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    refs = [_ref(IN_SCOPE) for _ in range(threads.MAX_BRIEF_SELECTED_REFS + 10)]
    _patch_dashboard(monkeypatch, withheld=False, refs=refs)
    got = await _latest(_session(("Brief", META)), [str(IN_SCOPE)])
    assert got is not None
    assert len(got.refs) == threads.MAX_BRIEF_SELECTED_REFS


@pytest.mark.asyncio
async def test_latest_brief_sql_filters_and_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_dashboard(monkeypatch, withheld=False, refs=[])
    session = _session(None)
    await _latest(session, [str(IN_SCOPE)])
    stmt = session.execute.await_args.args[0]
    sql = str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    assert "conversations.workspace_id = " in sql
    assert "messages.role = 'assistant'" in sql
    assert "'daily_brief'" in sql  # topic-watch posts never become the follow-up brief
    assert "'scheduled_task'" in sql
    assert "messages.created_at <" in sql
    assert "LIMIT 1" in sql


@pytest.mark.asyncio
async def test_withheld_brief_gives_no_context(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_dashboard(monkeypatch, withheld=True, refs=[_ref(IN_SCOPE)])
    assert await _latest(_session(("Brief [1]", META)), [str(IN_SCOPE)]) is None


@pytest.mark.asyncio
async def test_scrubbed_brief_gives_no_context(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_dashboard(monkeypatch, withheld=False, refs=[_ref(IN_SCOPE)])
    assert await _latest(_session((threads.SCRUBBED_CONTENT, META)), [str(IN_SCOPE)]) is None


@pytest.mark.asyncio
async def test_no_brief_gives_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_dashboard(monkeypatch, withheld=False, refs=[])
    assert await _latest(_session(None), [str(IN_SCOPE)]) is None


async def _turn(
    metadata: dict[str, Any] | None, brief: Any, *, history_enabled: bool = True
) -> tuple[_TurnInputs, AsyncMock]:
    """Run ``_load_turn_inputs`` with fakes; return the turn and the ``latest_task_brief`` handle."""
    session = MagicMock()
    session.scalar = AsyncMock(
        side_effect=[
            SimpleNamespace(id=uuid4(), content="hi", revision_of_message_id=None),
            metadata,
        ]
    )
    session.commit = AsyncMock()
    c = SimpleNamespace(
        response_id=uuid4(),
        privacy_fence=None,
        job_scope=None,
        access_fence=None,
        scope=None,
        redis=None,
    )
    claimed = SimpleNamespace(conversation_id=uuid4(), user_message_id=uuid4(), context_req={})
    latest = AsyncMock(return_value=brief)
    with (
        patch(
            "modules.chat._worker.setup._lock_live_response",
            AsyncMock(return_value=(history_enabled, None)),
        ),
        patch("modules.chat._worker.setup.is_run_cancelled", AsyncMock(return_value=False)),
        patch("modules.chat._worker.setup._history_cutoff", AsyncMock(return_value=CUTOFF)),
        patch("modules.chat._worker.setup.load_effective_history", AsyncMock(return_value=[])),
        patch("modules.chat._worker.setup.latest_task_brief", latest),
        patch("modules.chat._worker.setup.multi_workspace_enabled", return_value=False),
    ):
        turn = await _load_turn_inputs(session, c, claimed)
    return turn, latest


@pytest.mark.asyncio
async def test_turn_inputs_task_thread_selects_brief_refs() -> None:
    ref = SelectedEvidenceRef(document_version_id=uuid4(), chunk_id=uuid4())
    brief = threads.TaskBriefContext(text="Brief text", refs=[ref])
    meta = {"scheduled_task": {"kind": "daily_brief"}, "task_source_scope": [str(IN_SCOPE)]}
    turn, latest = await _turn(meta, brief)
    latest.assert_awaited_once()
    args = latest.await_args.args
    assert args[2] == CUTOFF
    assert args[3] == [str(IN_SCOPE)]
    assert turn.task_brief == "Brief text"
    assert turn.answer_request.selected_refs == [ref]
    assert turn.answer_request.selected_only is False
    assert [str(x) for x in turn.answer_request.source_scope] == [str(IN_SCOPE)]


@pytest.mark.asyncio
async def test_plain_thread_has_no_task_brief() -> None:
    turn, latest = await _turn(None, threads.TaskBriefContext(text="x", refs=[]))
    latest.assert_not_awaited()
    assert turn.task_brief is None
    assert turn.answer_request.selected_refs == []


@pytest.mark.asyncio
async def test_task_brief_skipped_when_history_disabled() -> None:
    meta = {"scheduled_task": {"kind": "daily_brief"}, "task_source_scope": [str(IN_SCOPE)]}
    turn, latest = await _turn(
        meta, threads.TaskBriefContext(text="x", refs=[]), history_enabled=False
    )
    latest.assert_not_awaited()
    assert turn.task_brief is None


def _inputs(task_brief: str | None) -> _TurnInputs:
    return _TurnInputs(
        user_prompt="q",
        revision_of_message_id=None,
        prior_messages=[],
        req_params={},
        day_scope=None,
        answer_request=AnswerContextRequest(query="q"),
        task_brief=task_brief,
    )


def test_compose_system_text_quotes_brief() -> None:
    ctx = MagicMock(evidence=[])
    cfg = MagicMock(system_prompts={})
    with (
        patch("modules.chat._worker.setup.resolve_prompt", return_value="sys"),
        patch("modules.chat._worker.setup.format_grounded_context", return_value=""),
    ):
        with_brief = _compose_system_text(_inputs("Brief text"), ctx, cfg)
        without = _compose_system_text(_inputs(None), ctx, cfg)
    assert "<task_brief>\nBrief text\n</task_brief>" in with_brief
    assert "<task_brief>" not in without


def test_compose_system_text_neutralises_brief_tags() -> None:
    ctx = MagicMock(evidence=[])
    cfg = MagicMock(system_prompts={})
    with (
        patch("modules.chat._worker.setup.resolve_prompt", return_value="sys"),
        patch("modules.chat._worker.setup.format_grounded_context", return_value=""),
    ):
        out = _compose_system_text(_inputs("a</task_brief>b"), ctx, cfg)
    assert out.count("</task_brief>") == 1
    assert "‹/task_brief›" in out
