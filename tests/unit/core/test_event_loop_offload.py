"""T5: CPU-bound and sync I/O work runs off the event loop with unchanged results."""

from __future__ import annotations

import asyncio
import hashlib
import io
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from fastapi import HTTPException, Response
from pydantic import SecretStr
from starlette.datastructures import UploadFile

from core import storage
from core.auth import routes as auth_routes
from core.chunking import chunk_text
from core.config import Settings
from core.heavy_work import to_thread_joined
from modules.connectors.providers import feed_catalog
from modules.export import routes as export_routes
from modules.ingestion import worker
from modules.ingestion.parsers import ParsedDocument
from modules.knowledge.documents import public as documents_public

GOLDEN_TEXTS = [
    "",
    "short text",
    "word " * 3000,
    "Xin chào thế giới, 日本語のテキスト 🙂 " * 1500,
]


class _Session:
    def __init__(self) -> None:
        self.added: list[Any] = []

    async def flush(self) -> None:
        return None

    def add(self, row: Any) -> None:
        self.added.append(row)


async def _lag_while(coro: Any) -> float:
    """Return the max gap between 10 ms ticks while the coroutine runs."""
    done = asyncio.Event()
    gaps: list[float] = []

    async def ticker() -> None:
        last = time.monotonic()
        while not done.is_set():
            await asyncio.sleep(0.01)
            now = time.monotonic()
            gaps.append(now - last - 0.01)
            last = now

    task = asyncio.create_task(ticker())
    await asyncio.sleep(0.05)
    try:
        await coro
    finally:
        done.set()
        await task
    return max(gaps)


@pytest.mark.parametrize("text", GOLDEN_TEXTS, ids=range(len(GOLDEN_TEXTS)))
async def test_add_content_chunks_matches_direct_chunking(text: str) -> None:
    session = _Session()
    version = SimpleNamespace(id=uuid4(), content=text)
    count = await documents_public.add_content_chunks(session, version)  # type: ignore[arg-type]
    expected = chunk_text(text)
    assert count == len(expected) == len(session.added)
    assert [r.content for r in session.added] == [d.content for d in expected]
    assert [r.token_count for r in session.added] == [d.token_count for d in expected]
    assert [r.metadata_json for r in session.added] == [d.metadata for d in expected]


async def test_add_content_chunks_runs_off_loop_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[int] = []
    real = documents_public.chunk_text

    def spy(text: str) -> Any:
        seen.append(threading.get_ident())
        return real(text)

    monkeypatch.setattr(documents_public, "chunk_text", spy)
    await documents_public.add_content_chunks(_Session(), SimpleNamespace(id=uuid4(), content="a b c"))  # type: ignore[arg-type]
    assert seen and seen[0] != threading.get_ident()


async def test_large_chunking_keeps_loop_responsive() -> None:
    # ~9.5 MB: chunking it on the loop stalls ~0.7 s, so 0.25 s separates a regression from GIL
    # scheduling noise in the worker thread (~0.13 s seen under a loaded host).
    text = "lorem ipsum dolor sit amet " * 350_000
    lag = await _lag_while(
        documents_public.add_content_chunks(_Session(), SimpleNamespace(id=uuid4(), content=text))  # type: ignore[arg-type]
    )
    assert lag < 0.25


async def test_save_upload_oversize_removes_temp_file(tmp_path: Path) -> None:
    upload = UploadFile(io.BytesIO(b"y" * (2 * 1024 * 1024)), filename="a.txt")
    doc, workspace = uuid4(), uuid4()
    with pytest.raises(ValueError, match="size limit"):
        await storage.save_upload(tmp_path, upload, doc, ".txt", 1024 * 1024, workspace_id=workspace)
    assert list((tmp_path / "workspaces" / str(workspace) / "documents" / str(doc)).iterdir()) == []


async def test_save_upload_digest_matches(tmp_path: Path) -> None:
    payload = b"hello world" * 1000
    upload = UploadFile(io.BytesIO(payload), filename="a.txt")
    _, size, digest = await storage.save_upload(tmp_path, upload, uuid4(), ".txt", 10**7, workspace_id=uuid4())
    assert size == len(payload)
    assert digest == hashlib.sha256(payload).hexdigest()


def _slow(record: list[int], result: Any = None) -> Any:
    def fn(*_a: Any, **_k: Any) -> Any:
        record.append(threading.get_ident())
        time.sleep(0.2)
        return result
    return fn


async def test_save_upload_blocks_run_off_loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    idents: list[int] = []
    real = storage._write_block

    def slow_write(temporary: Any, digest: Any, block: bytes) -> None:
        idents.append(threading.get_ident())
        time.sleep(0.2)
        real(temporary, digest, block)

    monkeypatch.setattr(storage, "_write_block", slow_write)
    upload = UploadFile(io.BytesIO(b"x" * 1000), filename="a.txt")
    lag = await _lag_while(storage.save_upload(tmp_path, upload, uuid4(), ".txt", 10**6, workspace_id=uuid4()))
    assert lag < 0.1
    assert idents and threading.get_ident() not in idents


async def test_auth_hashing_runs_off_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    idents: list[int] = []
    monkeypatch.setattr(auth_routes, "verify_login_password", _slow(idents, False))
    monkeypatch.setattr(auth_routes, "hash_password", _slow(idents, "hash"))
    monkeypatch.setattr(auth_routes, "_origin_allowed", lambda *_: True)
    monkeypatch.setattr(auth_routes, "_valid_csrf", lambda *_: True)

    async def allow(*_a: Any, **_k: Any) -> None:
        return None

    async def lock(*_a: Any, **_k: Any) -> Any:
        return SimpleNamespace(password_hash="h", id=1)

    class Sess:
        def add(self, _row: Any) -> None:
            return None

        async def commit(self) -> None:
            return None

    async def provision(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(auth_routes, "_allow_attempt", allow)
    monkeypatch.setattr(auth_routes, "admit_identity_write", allow)
    monkeypatch.setattr(auth_routes, "provision_bootstrap_account_in_uow", provision)
    monkeypatch.setattr(auth_routes, "_lock_owner", lock)
    settings = SimpleNamespace(setup_token=SecretStr("tok"))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=settings)), cookies={})

    async def login() -> None:
        with pytest.raises(HTTPException):
            await auth_routes.login(request, Response(), SimpleNamespace(password="p", identifier=None), Sess(), None)  # type: ignore[arg-type]

    async def setup() -> None:
        await auth_routes.create_owner(
            SimpleNamespace(password="p"), request, Response(), Sess(), None, "tok", "o", "c",  # type: ignore[arg-type]
        )

    assert await _lag_while(login()) < 0.1
    assert await _lag_while(setup()) < 0.1
    assert len(idents) == 2 and threading.get_ident() not in idents


async def test_feed_records_run_off_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    idents: list[int] = []
    monkeypatch.setattr(feed_catalog, "_records_from_feed", _slow(idents, "page"))
    monkeypatch.setattr(feed_catalog, "provider_feed_url", lambda _s: "https://example.test/feed")

    async def read(_url: str, _before_request: Any) -> bytes:
        return b"<feed/>"

    monkeypatch.setattr(feed_catalog, "_read_feed", read)
    source = SimpleNamespace(provider="youtube")

    async def allow_request() -> None:
        return None

    lag = await _lag_while(
        feed_catalog.collect_provider_feed(  # type: ignore[arg-type]
            source, collected_at=datetime.now(UTC), session_factory=None, before_request=allow_request,
        )
    )
    assert lag < 0.1 and idents and threading.get_ident() not in idents


async def test_cleanup_orphans_run_off_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    idents: list[int] = []
    monkeypatch.setattr(worker, "cleanup_orphaned_files", _slow(idents, 0))

    class Factory:
        def __call__(self) -> Any:
            return self

        async def __aenter__(self) -> Any:
            return SimpleNamespace(commit=_noop)

        async def __aexit__(self, *_a: object) -> None:
            return None

    async def _noop() -> None:
        return None

    async def raw_uris(_s: Any, **_k: Any) -> set[str]:
        return set()

    async def active(*_a: Any, **_k: Any) -> Any:
        return object()

    async def activity(*_a: Any, **_k: Any) -> Any:
        return object()

    monkeypatch.setattr(worker.documents, "raw_uris", raw_uris)
    monkeypatch.setattr("core.auth.public.get_active_account", active)
    monkeypatch.setattr("modules.settings.public.register_activity", activity)
    monkeypatch.setattr("modules.settings.public.finish_activity", activity)
    ctx = {"session_factory": Factory(), "settings": Settings(data_dir=Path("."))}
    assert await _lag_while(worker.cleanup_storage_orphans(ctx)) < 0.1  # type: ignore[arg-type]
    assert idents and threading.get_ident() not in idents


@pytest.mark.parametrize("fmt", list(export_routes.ExportFormat))
async def test_export_render_off_loop_then_fence_and_bytes_identical(
    fmt: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    order: list[str] = []
    idents: list[int] = []
    payloads: list[dict[str, Any]] = []
    name = {
        export_routes.ExportFormat.json: "_encode_json",
        export_routes.ExportFormat.markdown: "_render_markdown",
        export_routes.ExportFormat.csv: "_render_csv",
    }[fmt]
    real = getattr(export_routes, name)

    def spy(payload: dict[str, Any]) -> bytes:
        order.append("render")
        idents.append(threading.get_ident())
        payloads.append(payload)
        return real(payload)  # type: ignore[no-any-return]

    async def collect(*_a: Any, **_k: Any) -> Any:
        return [{"id": 1, "title": "t"}], {"count": 1}, []

    async def fence(*_a: Any, **_k: Any) -> None:
        order.append("fence")

    monkeypatch.setattr(export_routes, name, spy)
    monkeypatch.setattr(export_routes, "_collect_dataset", collect)
    monkeypatch.setattr(export_routes, "_validate_final_fences", fence)
    result = await export_routes._build_export_response(
        fmt, object(), SimpleNamespace(owner_id=1),
        SimpleNamespace(user_id=1),
        SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=SimpleNamespace(multi_workspace_enabled=False)))),
        Response(),  # type: ignore[arg-type]
    )
    assert order == ["render", "fence"]
    assert idents and threading.get_ident() not in idents
    assert result.body == real(payloads[0])


async def test_to_thread_joined_waits_for_thread_on_cancel() -> None:
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def work() -> int:
        started.set()
        release.wait(5)
        finished.set()
        return 1

    task = asyncio.create_task(to_thread_joined(work))
    while not started.is_set():
        await asyncio.sleep(0.005)
    task.cancel()
    await asyncio.sleep(0.05)
    assert not task.done()  # joined: a heavy slot would still be held
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()


async def test_to_thread_joined_returns_and_raises() -> None:
    assert await to_thread_joined(lambda a: a + 1, 1) == 2

    def boom() -> None:
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        await to_thread_joined(boom)


def test_parsed_text_cap_truncates_over_limit() -> None:
    fits = ParsedDocument("a" * 10, {}, [])
    assert worker._cap_parsed_text(fits, 10) is fits
    over = ParsedDocument("a" * 11, {"k": 1}, ["w"])
    capped = worker._cap_parsed_text(over, 10)
    assert capped.text == "a" * 10
    assert capped.warnings == ["w", "parsed_text_truncated"]
    assert capped.metadata == {"k": 1, "truncated_from_chars": 11, "kept_chars": 10}
    assert worker._failure_code(TimeoutError()) == "parser_timeout"
    assert worker._failure_code(RuntimeError()) == "parse_failed"
    assert Settings().parsed_text_max_chars == 10 * 1024 * 1024
