"""Per-turn ``@`` source picker: the server alone writes and validates ``source_scope``."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from core.errors import ConflictError
from modules.chat._worker.setup import _load_turn_inputs
from modules.chat.public import resolve_gadget_context

_GET = "modules.sources.public.get_source"


def _src(status: str = "active", local_only: bool = False) -> SimpleNamespace:
    return SimpleNamespace(status=status, local_only=local_only)


async def _resolve(context: dict[str, Any], sources: dict[str, Any | None]) -> dict[str, Any]:
    async def fake_get(_s: Any, sid: Any, **_kw: Any) -> Any:
        return sources.get(str(sid))

    with patch(_GET, side_effect=fake_get):
        return await resolve_gadget_context(
            AsyncMock(),
            context,
            scope=None,
            multi_workspace_enabled=False,  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_client_source_scope_is_not_passed_through() -> None:
    """A client-written source_scope never reaches the stored context."""
    out = await _resolve({"kind": "general", "source_scope": ["x"]}, {})
    assert "source_scope" not in out


@pytest.mark.asyncio
async def test_source_ids_keep_readable_drop_rest() -> None:
    """Active and paused survive; local-only, archived, foreign and malformed are dropped."""
    a, p, lo, ar, foreign = (str(uuid4()) for _ in range(5))
    sources = {
        a: _src(),
        p: _src("paused"),
        lo: _src(local_only=True),
        ar: _src("archived"),
    }
    out = await _resolve(
        {"kind": "general", "sourceIds": [a, a, p, lo, ar, foreign, "nope"]}, sources
    )
    assert out["source_scope"] == [a, p]
    assert "sourceIds" not in out


@pytest.mark.asyncio
async def test_all_invalid_source_ids_conflict() -> None:
    """Ids given but none usable -> 409 source_scope_unavailable."""
    with pytest.raises(ConflictError) as exc:
        await _resolve({"kind": "general", "sourceIds": [str(uuid4())]}, {})
    assert exc.value.detail["code"] == "source_scope_unavailable"


@pytest.mark.asyncio
async def test_no_source_ids_no_scope() -> None:
    """Without sourceIds the context has no source_scope key."""
    out = await _resolve({"kind": "general"}, {})
    assert "source_scope" not in out


@pytest.mark.asyncio
async def test_gadget_still_drops_paused() -> None:
    """Gadget contexts keep the active-only rule."""
    a, p = str(uuid4()), str(uuid4())
    out = await _resolve(
        {"kind": "gadget", "gadgetId": "g", "sourceIds": [a, p]}, {a: _src(), p: _src("paused")}
    )
    assert out["source_scope"] == [a]


@pytest.mark.asyncio
async def test_turn_inputs_enforce_server_written_scope() -> None:
    """A source_scope list in the stored context is a hard allow-list, even when empty."""
    session = MagicMock()
    session.scalar = AsyncMock(
        side_effect=[SimpleNamespace(id=uuid4(), content="hi", revision_of_message_id=None), None]
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
    claimed = SimpleNamespace(
        conversation_id=uuid4(), user_message_id=uuid4(), context_req={"source_scope": []}
    )
    with (
        patch(
            "modules.chat._worker.setup._lock_live_response", AsyncMock(return_value=(False, None))
        ),
        patch("modules.chat._worker.setup.is_run_cancelled", AsyncMock(return_value=False)),
        patch(
            "modules.chat._worker.setup._history_cutoff", AsyncMock(return_value=datetime.now(UTC))
        ),
        patch("modules.chat._worker.setup.latest_task_brief", AsyncMock(return_value=None)),
        patch("modules.chat._worker.setup.multi_workspace_enabled", return_value=False),
    ):
        turn = await _load_turn_inputs(session, c, claimed)
    assert turn.answer_request.source_scope_enforced is True
