"""P15-T8 (BM-20): chat attachments are ordinary Documents in one local-only "Chat attachments" source."""

from __future__ import annotations

import inspect
import io
import typing
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.testclient import TestClient
from starlette.datastructures import Headers

from core.body_limit import BodyLimitMiddleware
from core.workspaces.schemas import WorkspaceContext
from modules.chat import public as chat_public
from modules.ingestion import routes
from modules.sources import public as sources_public
from modules.sources.models import Source

WS = uuid4()
SCOPE = WorkspaceContext(user_id=7, workspace_id=WS, role="owner", membership_revision=3)
KW: dict[str, Any] = {"scope": SCOPE, "multi_workspace_enabled": False}
# reject_unsendable_selection/resolve_gadget_context gain scope kwargs in slice B1; these tests
# activate once that slice is merged (phase integration).
B1_READY = "scope" in inspect.signature(chat_public.reject_unsendable_selection).parameters
needs_b1 = pytest.mark.skipif(not B1_READY, reason="requires slice B1 scoped chat selection API")


def _request(max_bytes: int = 1024) -> Any:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        settings=SimpleNamespace(upload_max_bytes=max_bytes, data_dir="unused", multi_workspace_enabled=False),
    )))


def _upload(name: str, content: bytes, mime: str) -> UploadFile:
    return UploadFile(io.BytesIO(content), size=len(content), filename=name,
                      headers=Headers({"content-type": mime}))


# --- get-or-create: one local-only source, serialized by an advisory lock ---------------------

def _admit(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    admit = AsyncMock(return_value=SimpleNamespace(name="fence"))
    monkeypatch.setattr(sources_public, "_admit_source_scope", admit)
    return admit


async def test_get_or_create_returns_existing_source_after_taking_the_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    admit = _admit(monkeypatch)
    existing = Source(id=uuid4(), type="manual", name="Chat attachments", local_only=True,
                      configuration={"chat_attachments": True})
    session = MagicMock()
    session.execute = AsyncMock()
    session.scalar = AsyncMock(return_value=existing)
    session.commit = AsyncMock()

    assert await sources_public.get_or_create_chat_attachments_source(session, **KW) is existing
    assert admit.await_args.kwargs["lock"] is True  # access locks come before the advisory lock
    lock_sql = str(session.execute.await_args_list[0].args[0])
    assert "pg_advisory_xact_lock" in lock_sql  # taken before the lookup, so racers serialize
    session.add.assert_not_called()


async def test_get_or_create_creates_one_local_only_manual_source(monkeypatch: pytest.MonkeyPatch) -> None:
    _admit(monkeypatch)
    session = MagicMock()
    session.execute = AsyncMock()
    session.scalar = AsyncMock(return_value=None)
    session.flush = AsyncMock()
    session.refresh = AsyncMock()
    replay = AsyncMock()
    monkeypatch.setattr(sources_public, "commit_with_replay", replay)
    monkeypatch.setattr(sources_public, "make_source_change", MagicMock())

    source = await sources_public.get_or_create_chat_attachments_source(session, **KW)

    session.add.assert_called_once_with(source)
    assert source.type == "manual" and source.local_only is True and source.workspace_id == WS
    assert source.name == "Chat attachments" and source.configuration == {"chat_attachments": True}
    replay.assert_awaited_once()
    assert replay.await_args.kwargs["scope"] == SCOPE and "access_fence" in replay.await_args.kwargs
    # The lookup ignores archived (purged) sources, so a purge never resurrects the old one.
    lookup_sql = str(session.scalar.await_args.args[0])
    assert "sources.status !=" in lookup_sql and "configuration" in lookup_sql
    assert "sources.workspace_id" in lookup_sql  # no cross-workspace attachment source


async def test_marker_check_is_scoped_and_requires_the_server_set_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    get = AsyncMock(return_value=Source(configuration={"chat_attachments": True}))
    monkeypatch.setattr(sources_public, "get_source", get)
    assert await sources_public.is_chat_attachments_source(MagicMock(), uuid4(), **KW) is True
    assert get.await_args.kwargs == KW
    get.return_value = Source(configuration={})
    assert await sources_public.is_chat_attachments_source(MagicMock(), uuid4(), **KW) is False
    get.return_value = None
    assert await sources_public.is_chat_attachments_source(MagicMock(), uuid4(), **KW) is False


# --- boundary limits reuse the shared upload intake -------------------------------------------

def _intake_session(monkeypatch: pytest.MonkeyPatch, status: str = "active") -> MagicMock:
    access = SimpleNamespace(name="access")
    monkeypatch.setattr(routes, "lock_access_fence", AsyncMock(return_value=access))
    monkeypatch.setattr(routes, "authenticated_session_ref", MagicMock(return_value="ref"))
    fence = SimpleNamespace(status=status)
    monkeypatch.setattr(routes.sources, "lock_source_set", AsyncMock(
        return_value=SimpleNamespace(fences=[fence], access_fence=access)))
    monkeypatch.setattr(routes.sources, "get_source_fence", AsyncMock(return_value=fence))
    session = MagicMock()
    session.rollback = AsyncMock()
    return session


async def test_intake_rejects_oversized_upload_with_413(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _intake_session(monkeypatch)
    with pytest.raises(HTTPException) as caught:
        await routes._intake_upload(_request(10), session, SCOPE, uuid4(), _upload("a.txt", b"x" * 11, "text/plain"))
    assert caught.value.status_code == 413


async def test_intake_rejects_unsupported_type_with_415(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _intake_session(monkeypatch)
    with pytest.raises(HTTPException) as caught:
        await routes._intake_upload(
            _request(), session, SCOPE, uuid4(), _upload("a.exe", b"MZ", "application/x-msdownload"),
        )
    assert caught.value.status_code == 415


async def test_intake_into_paused_attachments_source_stays_a_concealing_404(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _intake_session(monkeypatch, status="paused")  # OB-2: port-ab intake, not P15's 409
    with pytest.raises(HTTPException) as caught:
        await routes._intake_upload(_request(), session, SCOPE, uuid4(), _upload("a.txt", b"hi", "text/plain"))
    assert caught.value.status_code == 404


async def test_intake_dedupe_returns_existing_document_id_and_unlinks_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _intake_session(monkeypatch)
    monkeypatch.setattr(routes, "save_upload", AsyncMock(return_value=("raw/x", 2, "abc")))
    unlink = MagicMock()
    monkeypatch.setattr(routes, "storage_path", MagicMock(return_value=SimpleNamespace(unlink=unlink)))
    monkeypatch.setattr("modules.settings.public.admit_write", AsyncMock())
    run = SimpleNamespace(id=uuid4())
    monkeypatch.setattr(routes.public, "receive_file", AsyncMock(return_value=(run, False)))
    existing = uuid4()
    find = AsyncMock(return_value=existing)
    monkeypatch.setattr(routes.documents, "find_document_identity", find)
    source_id = uuid4()

    got_run, document_id = await routes._intake_upload(
        _request(), session, SCOPE, source_id, _upload("a.txt", b"hi", "text/plain"),
    )

    assert got_run is run and document_id == existing
    assert find.await_args.args[1:] == (source_id, "file:abc") and find.await_args.kwargs == KW
    unlink.assert_called_once()
    find.return_value = None
    with pytest.raises(HTTPException) as caught:
        await routes._intake_upload(_request(), session, SCOPE, source_id, _upload("a.txt", b"hi", "text/plain"))
    assert caught.value.status_code == 409


async def test_attachment_upload_targets_server_chosen_source(monkeypatch: pytest.MonkeyPatch) -> None:
    source_id, document_id, run_id = uuid4(), uuid4(), uuid4()
    monkeypatch.setattr(routes.sources, "get_or_create_chat_attachments_source",
                        AsyncMock(return_value=SimpleNamespace(id=source_id)))
    intake = AsyncMock(return_value=(SimpleNamespace(id=run_id), document_id))
    monkeypatch.setattr(routes, "_intake_upload", intake)
    read = AsyncMock(return_value="read")
    monkeypatch.setattr(routes, "_chat_attachment_read", read)
    upload = _upload("a.txt", b"hello", "text/plain")

    assert await routes.upload_chat_attachment(_request(), upload, MagicMock(), SCOPE) == "read"
    assert intake.await_args.args[2:4] == (SCOPE, source_id)  # the same pipeline the manual upload route uses
    # Default is the private (local_only) source.
    assert routes.sources.get_or_create_chat_attachments_source.await_args.kwargs == {**KW, "shared": False}
    assert read.await_args.args[1:] == (document_id, run_id) and read.await_args.kwargs == KW


def test_attachment_routes_bind_the_default_workspace_dependencies() -> None:
    from core.workspaces.dependencies import (
        require_default_workspace_read,
        require_default_workspace_write,
    )

    def deps(route: Any) -> set[Any]:
        hint = typing.get_type_hints(route, include_extras=True)["_owner"]
        return {dep.dependency for dep in hint.__metadata__}

    assert deps(routes.upload_chat_attachment) == {require_default_workspace_write}
    assert deps(routes.get_chat_attachment) == {require_default_workspace_read}


async def test_get_attachment_requires_owner_role() -> None:
    member = WorkspaceContext(user_id=7, workspace_id=WS, role="member", membership_revision=3)
    with pytest.raises(HTTPException) as caught:
        await routes.get_chat_attachment(uuid4(), _request(), MagicMock(), member)
    assert caught.value.status_code == 403


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

    read = await routes._chat_attachment_read(MagicMock(), document.id, **KW)
    assert read.status == expected
    assert read.local_only is True  # local_only by default, reported to the composer
    assert read.document_version_id == (version_id if expected in {"ready", "too_large"} else None)


async def test_attachment_read_hides_documents_outside_the_attachments_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(routes.documents, "get_document", AsyncMock(return_value=_doc("succeeded")))
    monkeypatch.setattr(routes.sources, "is_chat_attachments_source", AsyncMock(return_value=False))
    with pytest.raises(HTTPException) as caught:
        await routes._chat_attachment_read(MagicMock(), uuid4(), **KW)
    assert caught.value.status_code == 404


async def test_attachment_read_of_a_foreign_workspace_document_is_404(monkeypatch: pytest.MonkeyPatch) -> None:
    get_document = AsyncMock(return_value=None)  # scoped get_document conceals foreign rows
    monkeypatch.setattr(routes.documents, "get_document", get_document)
    with pytest.raises(HTTPException) as caught:
        await routes._chat_attachment_read(MagicMock(), uuid4(), **KW)
    assert caught.value.status_code == 404
    assert get_document.await_args.kwargs == KW


# --- selection reference and send-time refusal ------------------------------------------------

@needs_b1
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

    monkeypatch.setattr(sources_public, "get_source", AsyncMock(return_value=Source(
        local_only=True, configuration={"chat_attachments": True})))
    with pytest.raises(HTTPException) as caught:
        await chat_public.reject_unsendable_selection(MagicMock(), resolved, **KW)
    assert caught.value.status_code == 409
    assert caught.value.detail["code"] == "selection_local_only"  # machine code the UI maps


@needs_b1
async def test_per_message_attachment_count_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sources_public, "get_source", AsyncMock(return_value=Source(
        local_only=False, configuration={"chat_attachments": True})))
    fences = [{"source_id": str(uuid4()), "local_only": False}
              for _ in range(chat_public.MAX_CHAT_ATTACHMENTS_PER_MESSAGE + 1)]
    with pytest.raises(HTTPException) as caught:
        await chat_public.reject_unsendable_selection(MagicMock(), {"selection_fences": fences}, **KW)
    assert caught.value.status_code == 422
    await chat_public.reject_unsendable_selection(MagicMock(), {"selection_fences": fences[:-1]}, **KW)
    await chat_public.reject_unsendable_selection(MagicMock(), {"kind": "page"}, **KW)  # non-selection untouched


# --- purge / export: no special casing ---------------------------------------------------------

def test_export_eligibility_does_not_filter_by_source_kind_or_privacy() -> None:
    sql = str(sources_public.export_eligible_source_ids(scope=SCOPE))
    assert "configuration" not in sql and "local_only" not in sql
    assert "source_purge_operations" in sql  # only unfinished purges are excluded


def test_attachments_source_is_a_plain_manual_source_row() -> None:
    """Purge/archive and Document deletion key on ``sources.id``/``documents.source_id`` only, so a
    marker in ``configuration`` is the sole difference and no cleanup path needs to know about it."""
    columns = {column.name for column in Source.__table__.columns}
    assert "configuration" in columns and "chat_attachments" not in columns


# --- Fix round 1: per-upload "share with model" routing between two server-owned sources ----------

@pytest.mark.parametrize(("shared", "name", "local_only"), [
    (False, "Chat attachments", True),
    (True, "Chat attachments (shared)", False),
])
async def test_get_or_create_variant_has_own_lock_lookup_and_privacy(
    monkeypatch: pytest.MonkeyPatch, shared: bool, name: str, local_only: bool,
) -> None:
    _admit(monkeypatch)
    session = MagicMock()
    session.execute = AsyncMock()
    session.scalar = AsyncMock(return_value=None)
    session.flush = AsyncMock()
    session.refresh = AsyncMock()
    monkeypatch.setattr(sources_public, "commit_with_replay", AsyncMock())
    monkeypatch.setattr(sources_public, "make_source_change", MagicMock())

    source = await sources_public.get_or_create_chat_attachments_source(session, shared=shared, **KW)

    assert source.name == name and source.local_only is local_only and source.type == "manual"
    assert source.configuration == {"chat_attachments": True}
    variant = "shared" if shared else "private"
    assert session.execute.await_args.args[1] == {"key": f"umwelt.sources.chat_attachments.{WS}.{variant}"}
    lookup = session.scalar.await_args.args[0].compile(compile_kwargs={"literal_binds": True})
    assert f"sources.local_only IS {'false' if shared else 'true'}" in str(lookup)


def _attachment_app(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, AsyncMock]:
    from core.database import get_session
    from core.workspaces.dependencies import require_default_workspace_write

    get_or_create = AsyncMock(return_value=SimpleNamespace(id=uuid4()))
    monkeypatch.setattr(routes.sources, "get_or_create_chat_attachments_source", get_or_create)
    intake = AsyncMock(return_value=(SimpleNamespace(id=uuid4()), uuid4()))
    monkeypatch.setattr(routes, "_intake_upload", intake)
    monkeypatch.setattr(routes, "_chat_attachment_read", AsyncMock(return_value={
        "document_id": str(uuid4()), "source_id": str(uuid4()), "title": "a.txt",
        "status": "pending", "local_only": True,
    }))
    app = FastAPI()
    app.state.settings = SimpleNamespace(multi_workspace_enabled=False)
    app.add_api_route("/upload", routes.upload_chat_attachment, methods=["POST"], status_code=202)
    app.dependency_overrides[get_session] = lambda: MagicMock()
    app.dependency_overrides[require_default_workspace_write] = lambda: SCOPE
    return TestClient(app), get_or_create


@pytest.mark.parametrize(("form", "shared"), [
    ({}, False),
    ({"share_with_model": "false"}, False),
    ({"share_with_model": "true"}, True),
])
def test_upload_routes_by_share_flag_and_defaults_private(
    monkeypatch: pytest.MonkeyPatch, form: dict[str, str], shared: bool,
) -> None:
    client, get_or_create = _attachment_app(monkeypatch)
    response = client.post("/upload", data=form, files={"file": ("a.txt", b"hi", "text/plain")})
    assert response.status_code == 202
    assert get_or_create.await_args.kwargs == {**KW, "shared": shared}


def test_client_cannot_choose_the_destination_source(monkeypatch: pytest.MonkeyPatch) -> None:
    client, get_or_create = _attachment_app(monkeypatch)
    chosen = uuid4()
    response = client.post(
        "/upload", data={"source_id": str(chosen)}, files={"file": ("a.txt", b"hi", "text/plain")},
    )
    assert response.status_code == 202
    server_source = get_or_create.return_value.id
    assert routes._intake_upload.await_args.args[3] == server_source != chosen  # type: ignore[attr-defined]
    assert get_or_create.await_args.kwargs == {**KW, "shared": False}


@needs_b1
async def test_shared_attachment_is_sendable_and_private_one_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    fence = {"selection_fences": [{"source_id": str(uuid4()), "local_only": False}]}
    monkeypatch.setattr(sources_public, "get_source", AsyncMock(return_value=Source(
        local_only=False, configuration={"chat_attachments": True})))
    await chat_public.reject_unsendable_selection(MagicMock(), fence, **KW)
    # A stale fence snapshot is not trusted: the source's current local_only decides.
    monkeypatch.setattr(sources_public, "get_source", AsyncMock(return_value=Source(
        local_only=True, configuration={"chat_attachments": True})))
    with pytest.raises(HTTPException) as caught:
        await chat_public.reject_unsendable_selection(MagicMock(), fence, **KW)
    assert caught.value.status_code == 409


def test_both_attachment_sources_stay_plain_rows_for_purge_export_and_deletion() -> None:
    """Purge, export and owner deletion key on ``sources.id`` with no kind/privacy/marker filter,
    so the private and the shared source are both covered without special casing."""
    sql = str(sources_public.export_eligible_source_ids(scope=SCOPE))
    assert "local_only" not in sql and "configuration" not in sql


# --- route wiring: the 409 happens before any ResponseRun, commit or dispatch -------------------

def _chat_route_mocks(monkeypatch: pytest.MonkeyPatch) -> tuple[MagicMock, AsyncMock]:
    from modules.chat import routes as chat_routes

    session = MagicMock()
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock()
    monkeypatch.setattr(chat_routes, "lock_export_privacy", AsyncMock())
    monkeypatch.setattr(chat_routes, "read_export_privacy", AsyncMock(
        return_value=SimpleNamespace(store_conversation_history=True)))
    monkeypatch.setattr(chat_routes, "_lock_conversation", AsyncMock(
        return_value=SimpleNamespace(ephemeral=False, expires_at=None)))
    monkeypatch.setattr(chat_routes, "_reject_expired_conversation", MagicMock())
    monkeypatch.setattr(chat_routes, "_reject_active_response", AsyncMock())
    dispatch = AsyncMock()
    monkeypatch.setattr(chat_routes, "_dispatch_response_run", dispatch)
    monkeypatch.setattr(sources_public, "get_source", AsyncMock(return_value=Source(
        local_only=True, configuration={"chat_attachments": True})))
    return session, dispatch


def _assert_nothing_persisted(session: MagicMock, dispatch: AsyncMock) -> None:
    from modules.chat.models import ResponseRun

    assert not any(isinstance(call.args[0], ResponseRun) for call in session.add.call_args_list)
    session.commit.assert_not_awaited()
    dispatch.assert_not_awaited()


@needs_b1
async def test_send_message_refuses_local_only_selection_before_any_run(monkeypatch: pytest.MonkeyPatch) -> None:
    from modules.chat import routes as chat_routes
    from modules.chat.schemas import SendMessageRequest

    session, dispatch = _chat_route_mocks(monkeypatch)
    session.scalar = AsyncMock(return_value=None)  # no idempotent replay
    monkeypatch.setattr(chat_public, "resolve_gadget_context", AsyncMock(return_value={
        "selection_fences": [{"source_id": str(uuid4()), "local_only": True}],
    }))
    payload = SendMessageRequest(content="hi", client_request_id="r1")

    with pytest.raises(HTTPException) as caught:
        await chat_routes.send_message(uuid4(), payload, MagicMock(), session, MagicMock())

    assert caught.value.status_code == 409 and caught.value.detail["code"] == "selection_local_only"
    _assert_nothing_persisted(session, dispatch)


@pytest.mark.parametrize("action", ["edit", "regenerate"])
@needs_b1
async def test_mutate_message_rechecks_current_local_only(monkeypatch: pytest.MonkeyPatch, action: str) -> None:
    import hashlib

    from modules.chat import routes as chat_routes
    from modules.chat.schemas import MessageMutationRequest

    session, dispatch = _chat_route_mocks(monkeypatch)
    target = SimpleNamespace(id=uuid4(), role="user" if action == "edit" else "assistant", content="old",
                             response_id=uuid4())
    # The stored snapshot says not local_only; the source is local_only now.
    original_run = SimpleNamespace(user_message_id=uuid4(), retrieval_context={
        "selection_fences": [{"source_id": str(uuid4()), "local_only": False}],
    })
    prompt = SimpleNamespace(content="old")
    session.scalar = AsyncMock(side_effect=[None, None, target, original_run, prompt])
    payload = MessageMutationRequest(
        action=action, base_content_hash=hashlib.sha256(b"old").hexdigest(), client_request_id="m1",
        content="new" if action == "edit" else None,
    )

    with pytest.raises(HTTPException) as caught:
        await chat_routes.mutate_message(uuid4(), target.id, payload, MagicMock(), session, MagicMock())

    assert caught.value.status_code == 409 and caught.value.detail["code"] == "selection_local_only"
    session.add.assert_not_called()  # no prompt, run or receipt
    _assert_nothing_persisted(session, dispatch)
