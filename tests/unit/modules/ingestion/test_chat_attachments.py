"""P15-T8 (BM-20): chat attachments are ordinary Documents in one local-only "Chat attachments" source."""

from __future__ import annotations

import io
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.testclient import TestClient
from starlette.datastructures import Headers

from core.body_limit import BodyLimitMiddleware
from modules.chat import public as chat_public
from modules.ingestion import routes
from modules.sources import public as sources_public
from modules.sources.models import Source


def _request(max_bytes: int = 1024) -> Any:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        settings=SimpleNamespace(upload_max_bytes=max_bytes, data_dir="unused"),
    )))


def _upload(name: str, content: bytes, mime: str) -> UploadFile:
    return UploadFile(io.BytesIO(content), size=len(content), filename=name,
                      headers=Headers({"content-type": mime}))


# --- get-or-create: one local-only source, serialized by an advisory lock ---------------------

async def test_get_or_create_returns_existing_source_after_taking_the_lock() -> None:
    existing = Source(id=uuid4(), type="manual", name="Chat attachments", local_only=True,
                      configuration={"chat_attachments": True})
    session = MagicMock()
    session.execute = AsyncMock()
    session.scalar = AsyncMock(return_value=existing)
    session.commit = AsyncMock()

    assert await sources_public.get_or_create_chat_attachments_source(session) is existing
    lock_sql = str(session.execute.await_args_list[0].args[0])
    assert "pg_advisory_xact_lock" in lock_sql  # taken before the lookup, so racers serialize
    session.add.assert_not_called()


async def test_get_or_create_creates_one_local_only_manual_source(monkeypatch: pytest.MonkeyPatch) -> None:
    session = MagicMock()
    session.execute = AsyncMock()
    session.scalar = AsyncMock(return_value=None)
    session.flush = AsyncMock()
    session.refresh = AsyncMock()
    replay = AsyncMock()
    monkeypatch.setattr(sources_public, "commit_with_replay", replay)
    monkeypatch.setattr(sources_public, "make_source_change", MagicMock())

    source = await sources_public.get_or_create_chat_attachments_source(session)

    session.add.assert_called_once_with(source)
    assert source.type == "manual" and source.local_only is True
    assert source.name == "Chat attachments" and source.configuration == {"chat_attachments": True}
    replay.assert_awaited_once()
    # The lookup ignores archived (purged) sources, so a purge never resurrects the old one.
    lookup_sql = str(session.scalar.await_args.args[0])
    assert "sources.status !=" in lookup_sql and "configuration" in lookup_sql


async def test_marker_check_requires_the_server_set_flag() -> None:
    session = MagicMock()
    session.get = AsyncMock(return_value=Source(configuration={"chat_attachments": True}))
    assert await sources_public.is_chat_attachments_source(session, uuid4()) is True
    session.get = AsyncMock(return_value=Source(configuration={}))
    assert await sources_public.is_chat_attachments_source(session, uuid4()) is False
    session.get = AsyncMock(return_value=None)
    assert await sources_public.is_chat_attachments_source(session, uuid4()) is False


# --- boundary limits reuse the shared upload intake -------------------------------------------

async def test_intake_rejects_oversized_upload_with_413() -> None:
    with pytest.raises(HTTPException) as caught:
        await routes._intake_upload(_request(10), MagicMock(), uuid4(), _upload("a.txt", b"x" * 11, "text/plain"))
    assert caught.value.status_code == 413


async def test_intake_rejects_unsupported_type_with_415() -> None:
    with pytest.raises(HTTPException) as caught:
        await routes._intake_upload(_request(), MagicMock(), uuid4(), _upload("a.exe", b"MZ", "application/x-msdownload"))
    assert caught.value.status_code == 415


async def test_attachment_upload_targets_server_chosen_source(monkeypatch: pytest.MonkeyPatch) -> None:
    source_id, document_id, run_id = uuid4(), uuid4(), uuid4()
    monkeypatch.setattr(routes.sources, "get_or_create_chat_attachments_source",
                        AsyncMock(return_value=SimpleNamespace(id=source_id)))
    intake = AsyncMock(return_value=(SimpleNamespace(id=run_id), document_id))
    monkeypatch.setattr(routes, "_intake_upload", intake)
    read = AsyncMock(return_value="read")
    monkeypatch.setattr(routes, "_chat_attachment_read", read)
    upload = _upload("a.txt", b"hello", "text/plain")

    assert await routes.upload_chat_attachment(_request(), upload, MagicMock(), MagicMock()) == "read"
    assert intake.await_args.args[2] == source_id  # the same pipeline the manual upload route uses
    assert read.await_args.args[1:] == (document_id, run_id)


def test_body_limit_gives_the_attachment_route_the_upload_cap() -> None:
    app = FastAPI()
    app.add_middleware(BodyLimitMiddleware, default_limit=100, upload_limit=1000)

    @app.post("/api/v1/documents/chat-attachments")
    async def echo(request: Request) -> dict[str, int]:
        return {"n": len(await request.body())}

    client = TestClient(app)
    assert client.post("/api/v1/documents/chat-attachments", content=b"x" * 900).json() == {"n": 900}
    assert client.post("/api/v1/documents/chat-attachments", content=b"x" * 1001).status_code == 413


# --- status projection ------------------------------------------------------------------------

def _doc(status: str) -> Any:
    return SimpleNamespace(id=uuid4(), source_id=uuid4(), title="notes.txt", extraction_status=status)


@pytest.mark.parametrize(
    ("extraction", "projection", "expected"),
    [
        ("queued", None, "pending"),
        ("processing", None, "pending"),
        ("succeeded", "ok", "ready"),
        ("succeeded", "truncated", "too_large"),
        ("succeeded", None, "failed"),
        ("failed", None, "failed"),
        ("needs_ocr", None, "failed"),
    ],
)
async def test_attachment_status_projection(
    monkeypatch: pytest.MonkeyPatch, extraction: str, projection: str | None, expected: str,
) -> None:
    document = _doc(extraction)
    version_id = uuid4()
    monkeypatch.setattr(routes.documents, "get_document", AsyncMock(return_value=document))
    monkeypatch.setattr(routes.sources, "is_chat_attachments_source", AsyncMock(return_value=True))
    monkeypatch.setattr(routes.sources, "get_source", AsyncMock(return_value=SimpleNamespace(local_only=True)))
    proj = None if projection is None else SimpleNamespace(
        chunks_truncated=projection == "truncated", document_version_id=version_id, local_only=True,
    )
    monkeypatch.setattr(routes.documents, "get_news_document_projection", AsyncMock(return_value=proj))

    read = await routes._chat_attachment_read(MagicMock(), document.id)
    assert read.status == expected
    assert read.local_only is True  # local_only by default, reported to the composer
    assert read.document_version_id == (version_id if expected in {"ready", "too_large"} else None)


async def test_attachment_read_hides_documents_outside_the_attachments_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(routes.documents, "get_document", AsyncMock(return_value=_doc("succeeded")))
    monkeypatch.setattr(routes.sources, "is_chat_attachments_source", AsyncMock(return_value=False))
    with pytest.raises(HTTPException) as caught:
        await routes._chat_attachment_read(MagicMock(), uuid4())
    assert caught.value.status_code == 404


# --- selection reference and send-time refusal ------------------------------------------------

async def test_attachment_becomes_a_selection_reference_and_local_only_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from modules.knowledge.documents import public as documents_public

    source_id, document_id, version_id, chunk_id = uuid4(), uuid4(), uuid4(), uuid4()
    projection = SimpleNamespace(
        document_id=document_id, document_version_id=version_id, source_id=source_id,
        current_source_generation=1, source_type="manual", provider=None, local_only=True,
        scope_discriminator=None, chunks_truncated=False, chunks=[SimpleNamespace(id=chunk_id)],
    )
    monkeypatch.setattr(documents_public, "get_news_document_projection", AsyncMock(return_value=projection))
    monkeypatch.setattr(documents_public, "validate_gadget_document_selection_fences", AsyncMock(return_value=True))
    context = {"kind": "selection", "items": [
        {"sourceId": str(source_id), "documentId": str(document_id), "documentVersionId": str(version_id)},
    ]}

    resolved = await chat_public.resolve_gadget_context(MagicMock(), context)
    assert resolved["selected_only"] is True
    assert resolved["selected_refs"][0]["chunk_id"] == str(chunk_id)
    assert resolved["selection_fences"][0]["local_only"] is True

    monkeypatch.setattr(sources_public, "is_chat_attachments_source", AsyncMock(return_value=True))
    with pytest.raises(HTTPException) as caught:
        await chat_public.reject_unsendable_selection(MagicMock(), resolved)
    assert caught.value.status_code == 409 and "local-only" in str(caught.value.detail)


async def test_per_message_attachment_count_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sources_public, "is_chat_attachments_source", AsyncMock(return_value=True))
    fences = [{"source_id": str(uuid4()), "local_only": False}
              for _ in range(chat_public.MAX_CHAT_ATTACHMENTS_PER_MESSAGE + 1)]
    with pytest.raises(HTTPException) as caught:
        await chat_public.reject_unsendable_selection(MagicMock(), {"selection_fences": fences})
    assert caught.value.status_code == 422
    await chat_public.reject_unsendable_selection(MagicMock(), {"selection_fences": fences[:-1]})
    await chat_public.reject_unsendable_selection(MagicMock(), {"kind": "page"})  # non-selection untouched


# --- purge / export: no special casing ---------------------------------------------------------

def test_export_eligibility_does_not_filter_by_source_kind_or_privacy() -> None:
    sql = str(sources_public.export_eligible_source_ids())
    assert "configuration" not in sql and "local_only" not in sql
    assert "source_purge_operations" in sql  # only unfinished purges are excluded


def test_attachments_source_is_a_plain_manual_source_row() -> None:
    """Purge/archive and Document deletion key on ``sources.id``/``documents.source_id`` only, so a
    marker in ``configuration`` is the sole difference and no cleanup path needs to know about it."""
    columns = {column.name for column in Source.__table__.columns}
    assert "configuration" in columns and "chat_attachments" not in columns
