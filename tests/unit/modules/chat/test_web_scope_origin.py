"""Web search is suppressed only for picker (``@``) source scopes, not gadget or task-thread scopes."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from modules.chat._worker import setup as worker_setup
from modules.chat.schemas import AnswerContext, AnswerContextRequest
from tests.unit.modules.chat._worker_patch import patch_worker

_WEB = {"requested": True}


def _rig(monkeypatch: pytest.MonkeyPatch, req_params: dict[str, Any], enforced: bool) -> Any:
    """Build a no-prior-message turn whose web search is opted in; returns the search mock."""

    async def fake_build(_s: Any, _f: Any, _r: Any, _cfg: Any, request: Any, _scope: Any) -> Any:
        return AnswerContext(query=request.query, evidence=[])

    started = AsyncMock(return_value=None)
    patch_worker(monkeypatch, "build_context", fake_build)
    patch_worker(monkeypatch, "_rewrite_and_search", started)
    patch_worker(monkeypatch, "_publish_status", AsyncMock())
    patch_worker(monkeypatch, "is_run_cancelled", AsyncMock(return_value=False))
    c = SimpleNamespace(
        session_factory=None,
        settings=None,
        redis=None,
        scope=None,
        conversation_id=uuid4(),
        access=None,
        response_id=uuid4(),
        seq=0,
        privacy_fence=None,
    )
    turn = SimpleNamespace(
        user_prompt="q",
        prior_messages=[],
        req_params={"_web_search": _WEB, **req_params},
        answer_request=AnswerContextRequest(query="q", source_scope_enforced=enforced),
    )
    session = SimpleNamespace(commit=AsyncMock())
    return worker_setup, session, c, turn, started


@pytest.mark.asyncio
async def test_picker_scope_suppresses_web_search(monkeypatch: pytest.MonkeyPatch) -> None:
    """A server-marked ``@`` picker turn never starts the opted-in web search."""
    setup, session, c, turn, started = _rig(
        monkeypatch, {"source_scope_origin": "picker", "source_scope": ["s"]}, enforced=True
    )
    _ctx, web_run = await setup._retrieve_with_web(session, c, turn)  # type: ignore[arg-type]
    assert web_run is None
    started.assert_not_called()


@pytest.mark.asyncio
async def test_gadget_scope_keeps_web_search(monkeypatch: pytest.MonkeyPatch) -> None:
    """A gadget turn (enforced scope, no picker marker) starts the web search as before 37a83b9e."""
    setup, session, c, turn, started = _rig(
        monkeypatch,
        {"kind": "gadget", "gadget_id": "g", "source_scope": ["s"]},
        enforced=True,
    )
    await setup._retrieve_with_web(session, c, turn)  # type: ignore[arg-type]
    started.assert_called_once()


@pytest.mark.asyncio
async def test_task_thread_scope_keeps_web_search(monkeypatch: pytest.MonkeyPatch) -> None:
    """A task thread (pinned enforced scope, no picker marker) starts the web search as before 37a83b9e."""
    setup, session, c, turn, started = _rig(monkeypatch, {"source_scope": []}, enforced=True)
    await setup._retrieve_with_web(session, c, turn)  # type: ignore[arg-type]
    started.assert_called_once()
