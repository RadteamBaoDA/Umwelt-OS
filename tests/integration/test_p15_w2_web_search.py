"""P15 W2: chat web search against the real database, with the fake provider behind the real SSRF transport.

Runs the chat worker in-process (the Compose chat-worker cannot reach the fake provider: the web search
transport is https-only and fake-model serves plain http). The provider is ``fake_model.app`` mounted through
``httpx.ASGITransport`` as the delegate of the real ``ApprovedEndpointTransport``; DNS answers come from a fake
resolver, so public, NAT64 and private answers are exercised without a network. The Model gateway is stubbed;
everything else (privacy key, Conversation/Run locks, ai_settings FOR SHARE, persistence) is real.
"""

import asyncio
import json
import os
import socket
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from core.config import Settings
from core.model_gateway.schemas import AISettingsUpdate, PrivacySettings
from core.model_gateway.transport import ApprovedEndpointTransport, approved_web_search_transport
from modules.chat import worker
from modules.chat.models import Conversation, Message, ResponseRun, StreamEvent
from modules.chat.routes import _privacy_fence
from modules.chat.schemas import WEB_SEARCH_OUTCOME_KEY, AnswerContext
from modules.chat.scope import owner_scope_kwargs
from modules.memory.public import lock_export_privacy, read_export_privacy
from modules.settings import public as settings_public

sys.path.insert(0, str(Path(__file__).parent))
import fake_model

pytestmark = pytest.mark.skipif(
    os.getenv("BBD_INTEGRATION") != "1", reason="requires disposable Compose test services"
)

ENDPOINT = "https://search.test"
KEY = os.getenv("TEST_AI_CREDENTIAL_ENCRYPTION_KEY", "ZmFrZS1tb2RlbC10ZXN0LWtleS0wMDAwMDAwMDAwMDA=")


class _Redis:
    """Just enough Redis for the worker: no cancel flags, an in-memory daily counter."""

    def __init__(self) -> None:
        self.counts: dict[str, int] = {}

    async def exists(self, *_keys: str) -> int:
        return 0

    def pipeline(self, transaction: bool = True) -> Any:
        outer = self

        class _Pipe:
            key = ""

            def incr(self, key: str) -> None:
                self.key = key

            def expire(self, *_a: Any) -> None:
                return None

            async def execute(self) -> list[int]:
                outer.counts[self.key] = outer.counts.get(self.key, 0) + 1
                return [outer.counts[self.key], True]

        return _Pipe()


def _settings() -> Settings:
    return Settings(
        csrf_signing_secret="s", ai_credential_encryption_key=KEY,
        ai_allowed_endpoint_hosts={"search.test"}, web_search_daily_limit=5,
    )


def _answer(ip: str) -> tuple[Any, ...]:
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    return (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, 443, 0, 0) if ":" in ip else (ip, 443))


def _fake_provider(endpoint: str, hosts: Any, cidrs: Any) -> ApprovedEndpointTransport:
    approved_web_search_transport(endpoint, hosts, cidrs)  # the real host/https policy check
    delegate = httpx.ASGITransport(app=fake_model.app)
    return ApprovedEndpointTransport(httpx.URL(endpoint), tuple(cidrs), delegate, allow_global=True)


@pytest.fixture
async def factory(
    committed_engine: AsyncEngine, ready_owner_client: object,  # owner + default workspace must exist
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Configure web search (Tavily shape, consent on) and restore the owner's ai_settings row afterwards."""
    async with committed_engine.connect() as connection:
        before = (await connection.execute(text("SELECT * FROM ai_settings WHERE owner_id = 1"))).mappings().first()
    cipher = Fernet(KEY.encode()).encrypt(b"fake-search-key").decode()
    destination = f"web-search:{settings_public._fingerprint(ENDPOINT)[:32]}"
    privacy = json.dumps({"allow_remote_web_search": True, "web_search_destinations": [destination]})
    async with committed_engine.begin() as connection:
        await connection.execute(text(
            "INSERT INTO ai_settings (workspace_id, owner_id, web_search_provider, web_search_endpoint, "
            "web_search_api_key_ciphertext, privacy) VALUES ((SELECT id FROM workspaces ORDER BY created_at LIMIT 1), 1, 'tavily', :e, :c, CAST(:p AS jsonb)) "
            "ON CONFLICT (owner_id) DO UPDATE SET web_search_provider = 'tavily', web_search_endpoint = :e, "
            "web_search_api_key_ciphertext = :c, privacy = CAST(:p AS jsonb)"
        ), {"e": ENDPOINT, "c": cipher, "p": privacy})
    fake_model.SEARCH_LOG.clear()
    try:
        yield async_sessionmaker(committed_engine, expire_on_commit=False)
    finally:
        async with committed_engine.begin() as connection:
            if before is None:
                await connection.execute(text("DELETE FROM ai_settings WHERE owner_id = 1"))
            else:
                await connection.execute(text(
                    "UPDATE ai_settings SET web_search_provider = :web_search_provider, "
                    "web_search_endpoint = :web_search_endpoint, "
                    "web_search_api_key_ciphertext = :web_search_api_key_ciphertext, privacy = CAST(:p AS jsonb) "
                    "WHERE owner_id = 1"
                ), {**before, "p": json.dumps(before["privacy"])})


async def _run(factory: async_sessionmaker[AsyncSession], message: str) -> tuple[UUID, UUID, Any]:
    async with factory() as session:
        privacy = await read_export_privacy(session, **await owner_scope_kwargs(session))
        ws_id, actor_id = (await session.execute(text(
            "SELECT id, owner_user_id FROM workspaces ORDER BY created_at LIMIT 1"))).one()
        conversation = Conversation(workspace_id=ws_id, actor_user_id=actor_id, title="p15-w2", ephemeral=not privacy.store_conversation_history)
        session.add(conversation)
        await session.flush()
        user = Message(conversation_id=conversation.id, role="user", content=message)
        session.add(user)
        await session.flush()
        run = ResponseRun(
            workspace_id=ws_id, actor_user_id=actor_id, conversation_id=conversation.id, user_message_id=user.id, status="pending",
            retrieval_context={"_chat_privacy_fence": _privacy_fence(privacy), "_web_search": {"requested": True}},
        )
        session.add(run)
        await session.commit()
        return conversation.id, run.id, _privacy_fence(privacy)


async def _admit(factory: async_sessionmaker[AsyncSession], run_id: UUID) -> dict[str, Any]:
    """Worker-style job scope and access fence for the run's stamped workspace/actor."""
    async with factory() as session:
        ws_id, actor_id = (await session.execute(text(
            "SELECT workspace_id, actor_user_id FROM chat_response_runs WHERE id = :i"), {"i": run_id})).one()
    scope, fence = await worker._admit_job(factory, ws_id, actor_id)
    return {"scope": scope, "access_fence": fence}


async def _cleanup(factory: async_sessionmaker[AsyncSession], conversation_id: UUID) -> None:
    async with factory() as session:
        await session.execute(text("DELETE FROM chat_conversations WHERE id = :i"), {"i": conversation_id})
        await session.commit()


async def test_end_to_end_search_sends_only_literal_query_and_persists_cited_result(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = "latest umwelt news [fake:cite=1]"
    conversation_id, run_id, _ = await _run(factory, message)

    async def stream(**kwargs: Any) -> AsyncIterator[str]:
        assert "Fake result one" in kwargs["messages"][0]["content"]
        yield 'data: {"choices": [{"delta": {"content": "News [1]"}}]}'

    monkeypatch.setattr(worker, "approved_web_search_transport", _fake_provider)
    monkeypatch.setattr(worker, "build_context", AsyncMock(return_value=AnswerContext(query=message, evidence=[])))
    monkeypatch.setattr(worker, "ModelGateway", lambda **_k: SimpleNamespace(stream=stream))
    try:
        with patch.object(asyncio.get_running_loop(), "getaddrinfo", AsyncMock(return_value=[_answer("93.184.216.34")])):
            await worker.run_response_generation(run_id, factory, _settings(), _Redis())  # type: ignore[arg-type]
        assert [entry["query"] for entry in fake_model.SEARCH_LOG] == [message]
        assert fake_model.SEARCH_LOG[0]["auth_present"] is True
        async with factory() as session:
            run = await session.get(ResponseRun, run_id)
            assert run is not None and run.status == "completed"
            assert run.retrieval_context[WEB_SEARCH_OUTCOME_KEY] == {"status": "used", "reason": None, "result_count": 2}
            answer = await session.get(Message, run.assistant_message_id)
            assert answer is not None and answer.content == "News [1]"
            assert [c["url"] for c in answer.citations] == ["https://example.com/one"]  # uncited result dropped
            kinds = list(await session.scalars(select(StreamEvent.event_type).where(
                StreamEvent.response_id == run_id).order_by(StreamEvent.seq)))
            assert kinds.index("web_search") < kinds.index("message.delta")
    finally:
        await _cleanup(factory, conversation_id)


async def test_same_url_results_persist_one_citation(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = "dup news [fake:search=dup]"
    conversation_id, run_id, _ = await _run(factory, message)

    async def stream(**kwargs: Any) -> AsyncIterator[str]:
        yield 'data: {"choices": [{"delta": {"content": "A [1] B [2]"}}]}'

    monkeypatch.setattr(worker, "approved_web_search_transport", _fake_provider)
    monkeypatch.setattr(worker, "build_context", AsyncMock(return_value=AnswerContext(query=message, evidence=[])))
    monkeypatch.setattr(worker, "ModelGateway", lambda **_k: SimpleNamespace(stream=stream))
    try:
        with patch.object(asyncio.get_running_loop(), "getaddrinfo", AsyncMock(return_value=[_answer("93.184.216.34")])):
            await worker.run_response_generation(run_id, factory, _settings(), _Redis())  # type: ignore[arg-type]
        async with factory() as session:
            run = await session.get(ResponseRun, run_id)
            assert run is not None and run.status == "completed"
            assert run.retrieval_context[WEB_SEARCH_OUTCOME_KEY]["result_count"] == 2
            answer = await session.get(Message, run.assistant_message_id)
            assert answer is not None and answer.content == "A [1] B [1]"
            assert [c["url"] for c in answer.citations] == ["https://example.org/two"]
            assert answer.citations[0]["title"] == "Dup A"
    finally:
        await _cleanup(factory, conversation_id)


async def test_consent_revoked_while_fence_waits_sends_nothing(
    factory: async_sessionmaker[AsyncSession], committed_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review P1-1: hold the privacy key, commit a revocation while the search fence waits, then release."""
    conversation_id, run_id, fence = await _run(factory, "revocation race")
    monkeypatch.setattr(worker, "approved_web_search_transport", _fake_provider)
    async with factory() as holder:
        await lock_export_privacy(holder)
        async with factory() as claim:  # the worker claims the run before searching
            await claim.execute(text("UPDATE chat_response_runs SET status = 'streaming' WHERE id = :i"), {"i": run_id})
            await claim.commit()
        task = asyncio.create_task(worker._search_for_run(
            "revocation race", [], run_id, conversation_id, fence, factory, _settings(), _Redis(), **await _admit(factory, run_id),  # type: ignore[arg-type]
        ))
        await asyncio.sleep(0.5)  # the fence is now blocked on the privacy key
        async with committed_engine.begin() as connection:  # save_ai_settings takes only the row lock
            await connection.execute(text(
                "UPDATE ai_settings SET privacy = privacy || '{\"allow_remote_web_search\": false}'::jsonb "
                "WHERE owner_id = 1"))
        await holder.rollback()
    try:
        with patch.object(asyncio.get_running_loop(), "getaddrinfo", AsyncMock(return_value=[_answer("93.184.216.34")])):
            outcome = (await task).outcome
        assert outcome == {"status": "skipped", "reason": "not_configured", "result_count": 0}
        assert fake_model.SEARCH_LOG == []
    finally:
        await _cleanup(factory, conversation_id)


async def test_save_after_fence_read_blocks_until_body_is_sent(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review P1-1/P2-1, the decisive direction: a real ``save_ai_settings`` revoking consent, issued after the
    fence has read consent, must not commit while the request body is unsent, and commits once it is handed over."""
    conversation_id, run_id, fence = await _run(factory, "blocked save")
    async with factory() as claim:
        await claim.execute(text("UPDATE chat_response_runs SET status = 'streaming' WHERE id = :i"), {"i": run_id})
        await claim.commit()
    parked, go = asyncio.Event(), asyncio.Event()

    class _Parked(httpx.ASGITransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            parked.set()  # consent was read; the body has not been iterated yet
            await asyncio.wait_for(go.wait(), 3)  # shorter than the 4 s fence deadline
            return await super().handle_async_request(request)

    def parked_provider(endpoint: str, hosts: Any, cidrs: Any) -> ApprovedEndpointTransport:
        approved_web_search_transport(endpoint, hosts, cidrs)
        return ApprovedEndpointTransport(
            httpx.URL(endpoint), tuple(cidrs), _Parked(app=fake_model.app), allow_global=True,
        )

    async def save_revocation() -> None:
        async with factory() as session:  # another session, exactly what PUT /ai does
            revision = await session.scalar(text("SELECT configuration_revision FROM ai_settings WHERE owner_id = 1"))
            scope = (await owner_scope_kwargs(session))["scope"]
            await settings_public.save_ai_settings(session, AISettingsUpdate(
                web_search_provider="tavily", web_search_endpoint=ENDPOINT,  # type: ignore[arg-type]
                privacy=PrivacySettings(allow_remote_web_search=False), expected_revision=revision,
            ), _settings(), scope=scope)
            await session.commit()

    monkeypatch.setattr(worker, "approved_web_search_transport", parked_provider)
    search = save = None
    try:
        with patch.object(asyncio.get_running_loop(), "getaddrinfo", AsyncMock(return_value=[_answer("93.184.216.34")])):
            search = asyncio.create_task(worker._search_for_run(
                "blocked save", [], run_id, conversation_id, fence, factory, _settings(), _Redis(), **await _admit(factory, run_id),  # type: ignore[arg-type]
            ))
            await asyncio.wait_for(parked.wait(), 3)
            save = asyncio.create_task(save_revocation())
            done, _ = await asyncio.wait({save}, timeout=1.0)
            assert not done, "save_ai_settings committed while the request body was still unsent"
            go.set()  # the delegate now iterates the body; the fence is released from there
            await asyncio.wait_for(save, 3)
            outcome = (await asyncio.wait_for(search, 3)).outcome
        assert outcome == {"status": "used", "reason": None, "result_count": 2}
        async with factory() as session:
            consent = await session.scalar(text("SELECT privacy->>'allow_remote_web_search' FROM ai_settings WHERE owner_id = 1"))
            assert consent == "false"
    finally:
        go.set()
        for task in (search, save):
            if task is not None and not task.done():
                task.cancel()
        await _cleanup(factory, conversation_id)


@pytest.mark.parametrize("ip", ["64:ff9b::a9fe:a9fe", "10.0.0.1", "169.254.169.254"])
async def test_nat64_and_private_answers_are_denied(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch, ip: str,
) -> None:
    conversation_id, run_id, fence = await _run(factory, "where is the metadata")
    async with factory() as claim:
        await claim.execute(text("UPDATE chat_response_runs SET status = 'streaming' WHERE id = :i"), {"i": run_id})
        await claim.commit()
    monkeypatch.setattr(worker, "approved_web_search_transport", _fake_provider)
    try:
        with patch.object(asyncio.get_running_loop(), "getaddrinfo", AsyncMock(return_value=[_answer(ip)])):
            run = await worker._search_for_run(
                "where is the metadata", [], run_id, conversation_id, fence, factory, _settings(), _Redis(), **await _admit(factory, run_id),  # type: ignore[arg-type]
            )
        assert run.outcome == {"status": "unavailable", "reason": "network_denied", "result_count": 0}
        assert fake_model.SEARCH_LOG == []
    finally:
        await _cleanup(factory, conversation_id)
