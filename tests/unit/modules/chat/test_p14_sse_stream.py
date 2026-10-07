"""P14-T4: chat SSE drains outside the transaction, writes one batch per locked transaction, caps streams;
conversation citations are filtered in one batch."""

import asyncio
from types import SimpleNamespace
from typing import Any, Self
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from core.config import Settings
from modules.chat import public as chat_public
from modules.chat import routes
from modules.chat.models import Conversation, ResponseRun, StreamEvent

RID = uuid4()
CID = uuid4()


def _event(seq: int, data: dict[str, Any] | None = None) -> Any:
    return SimpleNamespace(
        seq=seq, event_type="message.delta", event_id=f"{RID}:{seq}", data=data or {"text": f"t{seq}"},
    )


class _Store:
    """Shared fake DB: stream events, run status, and an ordered log of session/yield activity."""

    def __init__(self, events: list[Any], status: str = "streaming") -> None:
        self.events = events
        self.status = status
        self.log: list[str] = []
        self.seq_floors: list[int] = []


class _Session:
    def __init__(self, store: _Store) -> None:
        self.store = store

    async def __aenter__(self) -> Self:
        self.store.log.append("begin")
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        self.store.log.append("end")
        return False

    async def scalar(self, stmt: Any) -> Any:
        entity = stmt.column_descriptions[0]["entity"]
        if entity is Conversation:
            return SimpleNamespace(id=CID, ephemeral=False, expires_at=None)
        if entity is ResponseRun:
            return SimpleNamespace(
                id=RID, conversation_id=CID, status=self.store.status,
                retrieval_context={"_chat_privacy_fence": {}},
            )
        raise AssertionError(stmt)

    async def scalars(self, stmt: Any) -> Any:
        assert stmt.column_descriptions[0]["entity"] is StreamEvent
        params = stmt.compile().params
        floor = next(v for k, v in params.items() if k.startswith("seq"))
        limit = next(v for k, v in params.items() if k.startswith("param"))
        self.store.seq_floors.append(floor)
        rows = [e for e in self.store.events if e.seq > floor][:limit]
        return SimpleNamespace(all=lambda: rows)

    async def commit(self) -> None:
        self.store.log.append("commit")

    async def rollback(self) -> None:
        self.store.log.append("rollback")


def _install(monkeypatch: pytest.MonkeyPatch, store: _Store, *, auth: list[bool] | None = None) -> None:
    async def current(_request: Any) -> bool:
        return True

    async def lock(_session: Any) -> None:
        store.log.append("lock")

    async def fence(_session: Any, _stamp: Any) -> None:
        return None

    answers = list(auth or [])

    async def auth_row(_session: Any, _hash: Any) -> bool:
        return answers.pop(0) if answers else True

    async def no_citations(_session: Any, citations: Any) -> list[Any]:
        return list(citations) if isinstance(citations, list) else []

    monkeypatch.setattr(routes, "_session_is_current", current)
    monkeypatch.setattr(routes, "lock_export_privacy", lock)
    monkeypatch.setattr(routes, "_require_privacy_fence", fence)
    monkeypatch.setattr(routes, "_auth_row_current", auth_row)
    monkeypatch.setattr(chat_public, "filter_current_citations", no_citations)


def _request(store: _Store, semaphore: asyncio.Semaphore | None = None) -> Any:
    async def disconnected() -> bool:
        return False

    return SimpleNamespace(
        is_disconnected=disconnected,
        cookies={},
        app=SimpleNamespace(state=SimpleNamespace(
            session_factory=lambda: _Session(store),
            chat_streams=semaphore or asyncio.Semaphore(routes.MAX_CHAT_STREAMS_PER_API_PROCESS),
        )),
    )


async def _open(store: _Store, last_event_id: str | None = None, semaphore: asyncio.Semaphore | None = None) -> Any:
    return await routes.get_response_events(RID, _request(store, semaphore), last_event_id=last_event_id, cursor=None)


async def _next(response: StreamingResponse, store: _Store) -> str:
    chunk = await anext(response.body_iterator)  # type: ignore[call-overload]
    store.log.append(f"yield:{chunk[:12]!r}" if chunk else "yield:drain")
    return str(chunk)


async def test_drain_is_outside_txn_and_64_events_are_one_write_inside_it(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _Store([_event(n) for n in range(1, 101)])
    _install(monkeypatch, store)
    response = await _open(store)
    store.log.clear()  # drop the pre-stream existence check session

    assert await _next(response, store) == ""  # drain point
    assert store.log == ["yield:drain"]  # no transaction was open while draining
    batch = await _next(response, store)
    assert batch.count("event: message.delta") == routes.STREAM_BATCH_SIZE == 64
    assert f"id: {RID}:1\n" in batch and f"id: {RID}:64\n" in batch and f"{RID}:65\n" not in batch
    # The batch was handed to send() after lock+reads and BEFORE commit/end: written under the locks.
    assert store.log[:3] == ["yield:drain", "begin", "lock"]
    assert store.log[-1].startswith("yield:") and "commit" not in store.log

    assert await _next(response, store) == ""  # previous txn committed and closed before the next drain
    assert store.log[-3:] == ["commit", "end", "yield:drain"]
    rest = await _next(response, store)
    assert rest.count("event: message.delta") == 36 and f"id: {RID}:100\n" in rest
    await response.body_iterator.aclose()  # type: ignore[attr-defined]


async def test_last_event_id_resumes_after_that_seq(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _Store([_event(n) for n in range(1, 11)])
    _install(monkeypatch, store)
    response = await _open(store, last_event_id=f"{RID}:7")
    await _next(response, store)
    batch = await _next(response, store)
    assert store.seq_floors == [7]
    assert [line for line in batch.splitlines() if line.startswith("id:")] == [
        f"id: {RID}:8", f"id: {RID}:9", f"id: {RID}:10",
    ]
    await response.body_iterator.aclose()  # type: ignore[attr-defined]


async def test_auth_checked_in_the_publication_txn_and_revocation_stops_text(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _Store([_event(1)])
    _install(monkeypatch, store, auth=[False])
    response = await _open(store)
    chunks = [chunk async for chunk in response.body_iterator]  # type: ignore[attr-defined]
    assert chunks[-1] == routes.format_sse_event("status", {"status": "auth_expired"})
    assert not any("message.delta" in str(chunk) for chunk in chunks)


async def test_backoff_doubles_to_one_second_and_resets_after_a_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _Store([])
    _install(monkeypatch, store)
    sleeps: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)
        if len(sleeps) == 6:
            store.events = [_event(1)]  # an event arrives
        if len(sleeps) == 8:
            store.status = "completed"  # then the run ends with nothing new
        await real_sleep(0)

    monkeypatch.setattr(routes.asyncio, "sleep", fake_sleep)
    response = await _open(store)
    chunks = [chunk async for chunk in response.body_iterator]  # type: ignore[attr-defined]
    assert sleeps == [0.1, 0.2, 0.4, 0.8, 1.0, 1.0, 0.1, 0.2]
    assert sum("message.delta" in str(chunk) for chunk in chunks) == 1


async def test_65th_stream_gets_503_and_permits_are_released(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _Store([], status="completed")
    _install(monkeypatch, store)
    semaphore = asyncio.Semaphore(routes.MAX_CHAT_STREAMS_PER_API_PROCESS)
    responses = [await _open(store, semaphore=semaphore) for _ in range(64)]
    with pytest.raises(HTTPException) as exc:
        await _open(store, semaphore=semaphore)
    assert exc.value.status_code == 503 and exc.value.detail == "Chat stream limit reached"
    async for _chunk in responses[0].body_iterator:  # terminal run: the stream ends and frees its permit
        pass
    assert semaphore._value == 1
    await _open(store, semaphore=semaphore)


def test_api_process_chat_stream_cap_is_64() -> None:
    from apps.api.main import create_app

    app = create_app(Settings(csrf_signing_secret="s"))
    assert routes.MAX_CHAT_STREAMS_PER_API_PROCESS == 64
    assert app.state.chat_streams._value == 64


async def test_batch_citations_filtered_with_one_call(monkeypatch: pytest.MonkeyPatch) -> None:
    keep = {"documentVersionId": "v1", "keep": True}
    drop = {"documentVersionId": "v2"}
    store = _Store([
        _event(1, {"text": "a", "citations": [keep, drop]}),
        _event(2, {"text": "b"}),
        _event(3, {"text": "c", "citations": [drop]}),
    ])
    _install(monkeypatch, store)
    calls: list[list[Any]] = []

    async def spy(_session: Any, citations: Any) -> list[Any]:
        calls.append(list(citations))
        return [c for c in citations if c.get("keep")]

    monkeypatch.setattr(chat_public, "filter_current_citations", spy)
    response = await _open(store)
    await _next(response, store)
    batch = await _next(response, store)
    assert len(calls) == 1 and len(calls[0]) == 3
    frames = batch.split("\n\n")
    assert '"keep": true' in frames[0] or '"keep":true' in frames[0]
    assert "v2" not in batch and '"citations"' not in frames[1]
    assert '"citations": []' in frames[2] or '"citations":[]' in frames[2]
    await response.body_iterator.aclose()  # type: ignore[attr-defined]


def _cite(version: UUID, chunk: UUID, source: UUID, document: UUID) -> dict[str, object]:
    return {
        "sourceId": str(source), "documentId": str(document),
        "documentVersionId": str(version), "chunkId": str(chunk),
    }


async def test_conversation_citations_batched_identical_to_per_message(monkeypatch: pytest.MonkeyPatch) -> None:
    from modules.knowledge.documents import public as documents_public

    source, document = uuid4(), uuid4()
    pairs = [(uuid4(), uuid4()) for _ in range(150)]
    live = set(pairs[::2])  # every other chunk still exists
    lock_calls: list[int] = []

    async def lock_chunks(_session: Any, refs: list[tuple[UUID, UUID]], **_kw: Any) -> list[Any]:
        lock_calls.append(len(refs))
        return [
            SimpleNamespace(document_version_id=v, chunk_id=c, source_id=source, document_id=document)
            for v, c in refs if (v, c) in live
        ]

    monkeypatch.setattr(documents_public, "lock_chat_evidence_chunks", lock_chunks)
    messages: list[object] = [
        [_cite(v, c, source, document) for v, c in pairs[i:i + 30]] for i in range(0, 150, 30)
    ]
    messages += [[], {"not": "a list"}, ["junk", _cite(*pairs[0], uuid4(), document)]]

    per_message = [await chat_public.filter_current_citations(None, m) for m in messages]  # type: ignore[arg-type]
    old_calls = len(lock_calls)
    lock_calls.clear()
    batched = await routes._filter_citation_lists(None, messages)  # type: ignore[arg-type]

    assert batched == per_message
    assert old_calls == 6 and lock_calls == [100, 50]  # one lock call per 100 unique refs, not per message


async def test_pure_asgi_stack_passes_empty_drain_chunk_and_writes_before_generator_resumes() -> None:
    """Guard for the drain/write ordering through the real middleware stack (fails with BaseHTTPMiddleware)."""
    from apps.api.main import create_app

    app = create_app(Settings(csrf_signing_secret="s"))
    order: list[str] = []

    async def body() -> Any:
        yield ""
        order.append("after-drain-yield")
        yield "data: x\n\n"
        order.append("after-batch-yield")

    async def route() -> StreamingResponse:
        return StreamingResponse(body(), media_type="text/event-stream")

    app.add_api_route("/t4-probe", route)

    async def receive() -> dict[str, Any]:
        await asyncio.sleep(10)
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.body":
            order.append(f"send:{message['body']!r}")

    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"}, "http_version": "1.1",
        "method": "GET", "scheme": "http", "path": "/t4-probe", "raw_path": b"/t4-probe", "root_path": "",
        "query_string": b"", "headers": [], "client": ("127.0.0.1", 1), "server": ("127.0.0.1", 80),
        "state": {},
    }
    await app(scope, receive, send)
    assert order[:4] == ["send:b''", "after-drain-yield", "send:b'data: x\\n\\n'", "after-batch-yield"]
