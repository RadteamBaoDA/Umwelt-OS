"""Regression: upload rejections raised as ``DomainError`` must roll back and delete the stored raw blob."""

from __future__ import annotations

import contextlib
import io
from types import SimpleNamespace
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import UploadFile
from starlette.datastructures import Headers

from core.errors import ConflictError, NotFoundError
from core.storage import StoredObject
from core.workspaces.schemas import WorkspaceContext
from modules.ingestion import routes

WS = uuid4()
SCOPE = WorkspaceContext(user_id=7, workspace_id=WS, role="owner", membership_revision=3)


@contextlib.asynccontextmanager
async def _lease(*_a: Any, **_k: Any) -> Any:
    yield MagicMock()


class _Writer:
    """Stand-in for the lease-bound ``AsyncSession`` used only for the fence recheck."""

    def __init__(self, *_a: Any, **_k: Any) -> None:
        self.rollback = AsyncMock()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_a: object) -> None:
        return None


def _wire(monkeypatch: pytest.MonkeyPatch, failing: str, exc: Exception) -> tuple[Any, Any]:
    """Patch the intake collaborators; ``failing`` names the call (``receive_file`` or ``fence``) that raises ``exc``."""
    storage = SimpleNamespace(
        put_stream=AsyncMock(return_value=StoredObject(key="raw/k", size=3, sha256="d" * 64)),
        delete=AsyncMock(),
    )
    fence = SimpleNamespace(status="active")
    locked = SimpleNamespace(fences=[fence], access_fence=MagicMock())
    monkeypatch.setattr(routes, "get_storage_backend", lambda _s: storage)
    monkeypatch.setattr(routes, "authenticated_session_ref", lambda _r: "ref")
    monkeypatch.setattr(routes, "lock_access_fence", AsyncMock())
    monkeypatch.setattr(routes.sources, "lock_source_set", AsyncMock(return_value=locked))
    monkeypatch.setattr(routes.sources, "get_source_fence", AsyncMock(
        side_effect=exc if failing == "fence" else None, return_value=fence))
    monkeypatch.setattr(routes.public, "receive_file", AsyncMock(
        side_effect=exc if failing == "receive_file" else None))
    monkeypatch.setattr(routes, "workspace_storage_lease", _lease)
    monkeypatch.setattr(routes, "AsyncSession", _Writer)
    monkeypatch.setattr(routes, "to_thread_joined", AsyncMock(return_value=(".txt", "text/plain", "a.txt")))
    monkeypatch.setattr(routes, "iter_upload", lambda _u: iter([b"abc"]))
    session = MagicMock()
    session.rollback = AsyncMock()
    return storage, session


@pytest.mark.asyncio
@pytest.mark.parametrize(("failing", "exc"), [
    ("receive_file", ConflictError("Upload capture proof is stale")),
    ("receive_file", NotFoundError("Source not found")),
    ("fence", ConflictError("Source is not active")),
])
async def test_domain_error_rejection_cleans_up_raw_blob(
    monkeypatch: pytest.MonkeyPatch, failing: str, exc: Exception,
) -> None:
    """A ``DomainError`` from the publish step re-raises after ``rollback`` and ``storage.delete(raw_uri)``."""
    storage, session = _wire(monkeypatch, failing, exc)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        settings=SimpleNamespace(upload_max_bytes=1024, data_dir="x", multi_workspace_enabled=False),
        session_factory=MagicMock(),
    )))
    upload = UploadFile(io.BytesIO(b"abc"), size=3, filename="a.txt", headers=Headers({"content-type": "text/plain"}))
    with pytest.raises(type(exc)):
        await routes._intake_upload(request, session, SCOPE, uuid4(), upload)  # type: ignore[arg-type]
    storage.delete.assert_awaited_once_with("raw/k")
    assert session.rollback.await_count >= 2
