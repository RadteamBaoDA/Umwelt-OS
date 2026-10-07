"""T5: CPU-bound and sync I/O work runs off the event loop with unchanged results."""

from __future__ import annotations

import asyncio
import hashlib
import io
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from starlette.datastructures import Headers, UploadFile

from core import storage
from core.auth.service import hash_password, verify_password
from core.chunking import chunk_text
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
    text = "lorem ipsum dolor sit amet " * 150_000  # ~4 MB
    lag = await _lag_while(
        documents_public.add_content_chunks(_Session(), SimpleNamespace(id=uuid4(), content=text))  # type: ignore[arg-type]
    )
    assert lag < 0.1


async def test_argon2_runs_off_loop_and_still_verifies() -> None:
    hashed = await asyncio.to_thread(hash_password, "correct horse battery")
    lag = await _lag_while(asyncio.to_thread(verify_password, hashed, "correct horse battery"))
    assert lag < 0.1
    assert verify_password(hashed, "correct horse battery")
    assert not verify_password(hashed, "wrong")


async def test_save_upload_digest_size_and_loop_responsive(tmp_path: Path) -> None:
    payload = b"x" * (5 * 1024 * 1024 + 123)
    upload = UploadFile(io.BytesIO(payload), filename="a.txt", headers=Headers({"content-type": "text/plain"}))
    doc = uuid4()
    lag = await _lag_while(storage.save_upload(tmp_path, upload, doc, ".txt", 10 * 1024 * 1024))
    rel = f"documents/{doc}/{doc}.txt"
    assert (tmp_path / rel).read_bytes() == payload
    assert lag < 0.1


async def test_save_upload_oversize_removes_temp_file(tmp_path: Path) -> None:
    upload = UploadFile(io.BytesIO(b"y" * (2 * 1024 * 1024)), filename="a.txt")
    doc = uuid4()
    with pytest.raises(ValueError, match="size limit"):
        await storage.save_upload(tmp_path, upload, doc, ".txt", 1024 * 1024)
    assert list((tmp_path / "documents" / str(doc)).iterdir()) == []


async def test_save_upload_digest_matches(tmp_path: Path) -> None:
    payload = b"hello world" * 1000
    upload = UploadFile(io.BytesIO(payload), filename="a.txt")
    _, size, digest = await storage.save_upload(tmp_path, upload, uuid4(), ".txt", 10**7)
    assert size == len(payload)
    assert digest == hashlib.sha256(payload).hexdigest()
