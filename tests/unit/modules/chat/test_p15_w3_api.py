"""P15-W3: web_search API flag, per-run opt-in context, MessageRead outcome, SSE replay, citations, export."""

import hashlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from modules.chat import public as chat_public
from modules.chat import routes
from modules.chat.models import Message, ResponseRun, StreamEvent
from modules.chat.schemas import (
    ChatExportMessageRead,
    MessageMutationRequest,
    MessageRead,
    SendMessageRequest,
    WebCitation,
)
from modules.chat.stream import format_sse_event

NOW = datetime(2026, 10, 7, tzinfo=UTC)
WEB = {
    "sourceType": "web", "url": "https://example.com/a?q=secret#frag", "title": "Example",
    "quote": "snippet", "provider": "tavily", "retrievedAt": "2026-10-07T00:00:00Z",
}
HASH = "a" * 64


def test_schemas_accept_flag_default_off_and_stay_closed() -> None:
    assert SendMessageRequest(content="hi").web_search is False
    assert SendMessageRequest(content="hi", web_search=True).web_search is True
    mutation = {"action": "regenerate", "base_content_hash": HASH, "client_request_id": "r1"}
    assert MessageMutationRequest(**mutation).web_search is False  # type: ignore[arg-type]
    assert MessageMutationRequest(**mutation, web_search=True).web_search is True  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        SendMessageRequest(content="hi", websearch=True)  # type: ignore[call-arg]


def test_run_context_never_inherits_private_keys_and_writes_own_opt_in() -> None:
    privacy = SimpleNamespace(store_conversation_history=True, persisted=True, updated_at=NOW)
    original = {
        "source_scope": ["s1"],
        "_chat_privacy_fence": {"stale": True},
        "_web_search": {"requested": True},
        "_web_search_outcome": {"status": "used", "reason": None, "result_count": 3},
    }
    regenerated = routes._run_context(original, privacy, False)
    assert regenerated["source_scope"] == ["s1"]
    assert regenerated["_web_search"] == {"requested": False}  # not inherited, rewritten even when false
    assert "_web_search_outcome" not in regenerated
    assert regenerated["_chat_privacy_fence"]["updated_at"] == NOW.isoformat()
    assert routes._run_context(original, privacy, True)["_web_search"] == {"requested": True}
    assert routes._run_context(None, privacy, False)["_web_search"] == {"requested": False}


def test_digest_changes_with_flag_but_old_receipts_stay_valid() -> None:
    base: dict[str, Any] = {
        "action": "edit", "target_message_id": uuid4(), "base_content_hash": HASH, "content": "x",
    }
    off = routes._message_mutation_digest(**base)
    assert off != routes._message_mutation_digest(**base, web_search=True)
    assert off == routes._message_mutation_digest(**base, web_search=False)
    legacy = json.dumps(
        {**base, "target_message_id": str(base["target_message_id"])},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )
    assert off == hashlib.sha256(legacy.encode()).hexdigest()  # pre-deploy receipts still match


def test_message_read_web_search_shape() -> None:
    common: dict[str, Any] = {
        "id": uuid4(), "conversation_id": uuid4(), "role": "assistant", "content": "a", "created_at": NOW,
    }
    assert MessageRead(**common).web_search is None
    dumped = MessageRead(
        **common, web_search={"status": "unavailable", "reason": "timeout", "result_count": 0},
    ).model_dump(mode="json")
    assert dumped["web_search"] == {"status": "unavailable", "reason": "timeout", "result_count": 0}
    with pytest.raises(ValidationError):
        MessageRead(**common, web_search={"status": "bogus", "reason": None, "result_count": 0})


class _Rows:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows


class _ConversationSession:
    def __init__(self, messages: list[Any], outcomes: list[tuple[Any, Any]]) -> None:
        self.messages, self.outcomes, self.executes = messages, outcomes, 0
        self.conv = SimpleNamespace(
            id=messages[0].conversation_id, title="t", context_kind=None, context_resource_id=None,
            pinned=False, archived=False, created_at=NOW, updated_at=NOW, metadata_json={},
            ephemeral=False, expires_at=None,
        )

    async def scalar(self, stmt: Any) -> Any:
        entity = stmt.column_descriptions[0]["entity"]
        return self.conv if entity.__name__ == "Conversation" else uuid4()  # active run id: skips reload

    async def scalars(self, _stmt: Any) -> _Rows:
        return _Rows(self.messages)

    async def execute(self, _stmt: Any) -> _Rows:
        self.executes += 1
        return _Rows([] if self.executes == 1 else self.outcomes)


async def test_get_conversation_reads_outcome_for_assistant_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    cid, assistant_id, other_id = uuid4(), uuid4(), uuid4()

    def msg(mid: Any, role: str) -> Any:
        return SimpleNamespace(
            id=mid, conversation_id=cid, role=role, content="c", client_request_id=None, model_identity=None,
            citations=[], response_id=None, revision_of_message_id=None, created_at=NOW,
        )

    session = _ConversationSession(
        [msg(uuid4(), "user"), msg(assistant_id, "assistant"), msg(other_id, "assistant")],
        [(assistant_id, {"status": "skipped", "reason": "daily_limit", "result_count": 0}),
         (other_id, {"status": "garbage"})],
    )

    async def keep(_session: Any, lists: Any) -> list[list[Any]]:
        return [list(x) for x in lists]

    monkeypatch.setattr(routes, "_filter_citation_lists", keep)
    detail = await routes.get_conversation(cid, session, None, SimpleNamespace(headers={}))  # type: ignore[arg-type]
    by_id = {m.id: m.web_search for m in detail.messages}
    assert by_id[assistant_id] is not None and by_id[assistant_id].reason == "daily_limit"
    assert by_id[other_id] is None  # malformed stored outcome reports nothing
    assert [m.web_search for m in detail.messages if m.role == "user"] == [None]


def test_web_search_event_frame_shape() -> None:
    data = {"status": "used", "reason": None, "result_count": 2}
    frame = format_sse_event(event="web_search", data=data, event_id="r:3")
    assert frame == 'id: r:3\nevent: web_search\ndata: {"status":"used","reason":null,"result_count":2}\n\n'


async def test_sse_poll_replays_web_search_event(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.unit.modules.chat import test_p14_sse_stream as sse

    web = SimpleNamespace(
        seq=1, event_type="web_search", event_id=f"{sse.RID}:1",
        data={"status": "skipped", "reason": "not_configured", "result_count": 0},
    )
    store = sse._Store([web, sse._event(2)])
    sse._install(monkeypatch, store)
    response = await sse._open(store)
    await sse._next(response, store)
    batch = await sse._next(response, store)
    assert batch.startswith(f"id: {sse.RID}:1\nevent: web_search\n")
    assert '"reason":"not_configured"' in batch and "event: message.delta" in batch
    await response.body_iterator.aclose()  # type: ignore[attr-defined]


async def test_filter_current_citations_passes_web_through_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    from modules.knowledge.documents import public as documents_public

    source, document, version, chunk = uuid4(), uuid4(), uuid4(), uuid4()
    doc = {"sourceId": str(source), "documentId": str(document), "documentVersionId": str(version),
           "chunkId": str(chunk)}
    gone = {**doc, "chunkId": str(uuid4())}
    calls: list[int] = []

    async def lock(_s: Any, refs: Any, **_k: Any) -> list[Any]:
        calls.append(len(refs))
        return [SimpleNamespace(document_version_id=version, chunk_id=chunk, source_id=source, document_id=document)]

    monkeypatch.setattr(documents_public, "lock_chat_evidence_chunks", lock)
    web2 = {**WEB, "url": "https://example.org/b"}
    malformed = {"sourceType": "web", "url": "javascript:x"}  # missing title/provider/retrievedAt
    forged = {**WEB, "documentId": str(document)}  # mixed shape is rejected by extra="forbid"
    out = await chat_public.filter_current_citations(None, [web2, gone, doc, malformed, "junk", forged, WEB])  # type: ignore[arg-type]
    assert out == [web2, doc, WEB]
    assert out[0] is web2 and out[2] is WEB  # unchanged objects, original order
    calls.clear()
    assert await chat_public.filter_current_citations(None, [WEB]) == [WEB]  # type: ignore[arg-type]
    assert calls == []  # web-only lists take no evidence lock


def test_web_citation_serializes_camel_case() -> None:
    dumped = WebCitation.model_validate(WEB).model_dump(mode="json")
    assert set(dumped) == {"sourceType", "url", "title", "quote", "provider", "retrievedAt"}
    assert dumped["sourceType"] == "web" and dumped["retrievedAt"].startswith("2026-10-07T00:00:00")


class _Stream:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def mappings(self) -> "_Stream":
        return self

    def __aiter__(self) -> Any:
        async def gen() -> Any:
            for row in self._rows:
                yield row

        return gen()

    async def close(self) -> None:
        return None


async def test_export_emits_web_citations_with_query_stripped(monkeypatch: pytest.MonkeyPatch) -> None:
    async def ok(*_a: Any, **_k: Any) -> None:
        return None

    async def privacy(_s: Any) -> tuple[bool, bool, datetime | None]:
        return True, False, None

    async def count(*_a: Any) -> int:
        return 1

    async def no_evidence(*_a: Any) -> dict[Any, Any]:
        return {}

    ftp = {**WEB, "url": "ftp://x/y"}
    row = {
        "message_id": uuid4(), "conversation_id": uuid4(), "role": "assistant", "content": "answer [1]",
        "citations": [WEB, ftp, {"junk": 1}],
        "response_id": None, "revision_of_message_id": None,
        "message_created_at": NOW, "message_updated_at": NOW,
        "conversation_created_at": NOW, "conversation_updated_at": NOW,
    }

    class _Session:
        async def stream(self, _stmt: Any, *_a: Any, **_k: Any) -> _Stream:
            return _Stream([row])

    monkeypatch.setattr(chat_public, "_require_chat_export_owner", ok)
    monkeypatch.setattr(chat_public, "_chat_export_privacy", privacy)
    monkeypatch.setattr(chat_public, "_chat_export_count", count)
    monkeypatch.setattr(chat_public, "_chat_export_evidence_fences", no_evidence)
    page = await chat_public.export_page(_Session(), owner_id=1, record_kind="messages", limit=10)  # type: ignore[arg-type]
    item = page.items[0]
    assert isinstance(item, ChatExportMessageRead)
    assert [c.model_dump(mode="json") for c in item.citations] == [{
        "source_type": "web", "url": "https://example.com/a", "title": "Example", "quote": "snippet",
        "provider": "tavily", "retrieved_at": "2026-10-07T00:00:00Z",
    }]
    assert item.omitted_citation_count == 2  # unsafe URL and junk; the valid web item is never omitted
    assert page.fences[0].citations == []  # no evidence fence for web citations
    assert ChatExportMessageRead.model_validate(item.model_dump(mode="json")).citations == item.citations


def test_web_citations_ride_existing_jsonb_columns_that_cascade_on_delete() -> None:
    """Deletion and pg_dump backup need no new code: web citations live only in JSONB on cascading rows."""
    from sqlalchemy.dialects.postgresql import JSONB

    from core.database import Base

    assert isinstance(Message.__table__.c.citations.type, JSONB)  # type: ignore[attr-defined]
    assert isinstance(ResponseRun.__table__.c.citations.type, JSONB)  # type: ignore[attr-defined]
    assert isinstance(ResponseRun.__table__.c.retrieval_context.type, JSONB)  # type: ignore[attr-defined]
    assert isinstance(StreamEvent.__table__.c.data.type, JSONB)  # type: ignore[attr-defined]
    for table, column in (
        (Message.__table__, "conversation_id"),  # type: ignore[attr-defined]
        (ResponseRun.__table__, "conversation_id"),  # type: ignore[attr-defined]
        (StreamEvent.__table__, "response_id"),  # type: ignore[attr-defined]
    ):
        assert next(iter(table.c[column].foreign_keys)).ondelete == "CASCADE"
    assert not [t for t in Base.metadata.tables if t.startswith("chat") and "web" in t]  # no separate web table


async def test_delete_conversation_deletes_only_the_cascading_parent(monkeypatch: pytest.MonkeyPatch) -> None:
    import modules.agents.public as agents_public
    import modules.memory.public as memory_public

    deleted: list[Any] = []
    convo = SimpleNamespace(id=uuid4(), pinned=False)

    class _Session:
        async def scalar(self, stmt: Any) -> Any:
            return convo if stmt.column_descriptions[0]["entity"].__name__ == "Conversation" else 1

        async def delete(self, obj: Any) -> None:
            deleted.append(obj)

    async def noop(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(memory_public, "lock_export_privacy", noop)
    monkeypatch.setattr(agents_public, "purge_conversation_actions", noop)
    assert await chat_public.delete_conversation(_Session(), convo.id, 1) is True  # type: ignore[arg-type]
    assert deleted == [convo]  # messages, runs, events and their web citations go via ON DELETE CASCADE
