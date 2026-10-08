"""W2-S per-workspace module gate on ingestion entrypoints, against the real module registry (no DB)."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from core.workspaces.schemas import InternalJobScope
from modules.ingestion import public as ingestion_api
from modules.ingestion import worker

SCOPE = InternalJobScope(workspace_id=uuid4(), actor_user_id=7, membership_revision=3, source_id=uuid4(), source_generation=1)


def _factory(session: MagicMock) -> MagicMock:
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=ctx)


def _ctx(session: MagicMock) -> dict[str, Any]:
    return {"session_factory": _factory(session), "settings": SimpleNamespace(multi_workspace_enabled=False),
            "multi_workspace_enabled": False}


@pytest.mark.parametrize("entrypoint", [worker.process_ingestion_event, worker.process_uploaded_file])
@pytest.mark.parametrize("disabled", [True, False])
async def test_ingestion_entrypoints_check_ingestion_module(entrypoint: Any, disabled: bool) -> None:
    session = MagicMock(commit=AsyncMock(), rollback=AsyncMock())
    event = SimpleNamespace(payload={"document_id": str(uuid4())})
    work = SimpleNamespace(scope=SCOPE, run=MagicMock(), stage=SimpleNamespace(status="succeeded"), event=event,
                           state=None, source=SimpleNamespace(id=uuid4()))
    delivered = AsyncMock()
    with (
        patch.object(worker, "_lock_worker_event", AsyncMock(return_value=work)),
        patch.object(worker, "_worker_flag", return_value=False),
        patch.object(ingestion_api, "mark_event_delivered", delivered),
        # Real registry: only the persisted disable set is faked, so "ingestion" must be a valid module id.
        patch("modules.settings.lifecycle.get_disabled_modules",
              AsyncMock(return_value=({"ingestion"} if disabled else set(), 1, True))),
    ):
        # Heavy-work slot wrapper is infrastructure; call the wrapped consumer when present.
        await getattr(entrypoint, "__wrapped__", entrypoint)(_ctx(session), str(uuid4()))
    assert delivered.await_count == (0 if disabled else 1)
    assert session.commit.await_count == (0 if disabled else 1)
