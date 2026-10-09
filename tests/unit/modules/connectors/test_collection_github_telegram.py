"""GitHub and Telegram run natively through the shared executor (mocked transports, no database)."""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from modules.connectors import collection, provisioning
from modules.connectors import public as connectors
from modules.ingestion import public as ingestion
from tests.unit.modules.connectors.test_collection import attempt, factory

SNAPSHOT = SimpleNamespace(
    state="ready", encrypted_token="x", verified_bot_id="7", operation_id=uuid4(), access_fence=None)
LEASE = SimpleNamespace(
    token=uuid4(), cursor_before=None, source_generation=1, connector_revision=2, configuration_revision=None)


def make_run(provider, **extras):
    att = attempt(provider)
    att.source.configuration["telegram_chat_ids"] = ["1"]
    run = collection.Run(
        ctx={}, factory=factory([]), settings=SimpleNamespace(
            connector_credential_encryption_key=SimpleNamespace(get_secret_value=lambda: "k")),
        attempt=att, gate=None)
    run.extras.update(extras)
    return run


def test_github_and_telegram_are_native_not_refused():
    assert not collection.REFUSED_HERE
    assert collection.ADAPTERS["github"] is collection._run_github
    assert collection.ADAPTERS["telegram"] is collection._run_telegram
    for provider in ("github", "telegram"):
        assert collection.supports_native(SimpleNamespace(provider=provider, type="api"))


@pytest.mark.asyncio
async def test_credential_revision_change_blocks_the_send(monkeypatch):
    run = make_run("github", lease=LEASE)

    async def lock(*_a, **_k):
        return run.attempt.source_fence, SimpleNamespace(credential_revision=9), []

    monkeypatch.setattr(provisioning, "lock_connector", lock)
    with pytest.raises(collection.CollectionFenceLost) as lost:
        await collection._credential_fence(run, telegram=SNAPSHOT)
    assert lost.value.cancel_as == "revision_changed"


@pytest.mark.asyncio
async def test_telegram_missing_bot_credential_sends_nothing(monkeypatch):
    run = make_run("telegram")

    async def lease(_run):
        _run.extras["lease"] = LEASE
        return LEASE

    async def lock(*_a, **_k):
        return None, None, []

    async def snapshot(*_a, **_k):
        return None

    monkeypatch.setattr(collection, "_lease", lease)
    monkeypatch.setattr(provisioning, "lock_connector", lock)
    monkeypatch.setattr(connectors, "get_native_credential_snapshot", snapshot)
    with pytest.raises(collection.CredentialMissing):
        await collection._run_telegram(run)


@pytest.mark.asyncio
async def test_telegram_page_is_fenced_then_accepted_with_native_operation(monkeypatch):
    run = make_run("telegram")
    seen = {}
    page = SimpleNamespace(deliveries=(), collected_at=datetime.now(UTC), transport_bytes=10)

    async def lease(_run):
        _run.extras["lease"] = LEASE
        return LEASE

    async def none(*_a, **_k):
        return None

    async def lock(*_a, **_k):
        return None, None, []

    async def snapshot(*_a, **_k):
        return SNAPSHOT

    async def fetch(token, *, offset, remaining_bytes, before_request):
        seen["token"] = token
        return page

    async def accept(session, batch, **kwargs):
        seen["batch"], seen["kwargs"] = batch, kwargs

    async def gate():
        return None

    run.gate = gate
    fences = []

    async def fence(_run, **kw):
        fences.append(kw)

    monkeypatch.setattr(collection, "_lease", lease)
    monkeypatch.setattr(collection, "_credential_fence", fence)
    monkeypatch.setattr(provisioning, "lock_connector", lock)
    monkeypatch.setattr(connectors, "get_native_credential_snapshot", snapshot)
    monkeypatch.setattr(ingestion, "read_telegram_collection_state", none)
    monkeypatch.setattr(ingestion, "accept_native_collection", accept)
    monkeypatch.setattr("modules.connectors.credentials.decrypt_native_token", lambda key, snap: "123:abc")
    monkeypatch.setattr("modules.connectors.providers.telegram.fetch_telegram_updates", fetch)
    await collection._run_telegram(run)
    assert seen["token"] == "123:abc" and fences  # fenced before the accept
    assert seen["kwargs"]["expected_native_operation_id"] == SNAPSHOT.operation_id
    assert seen["kwargs"]["request_ref"] is run.attempt.ref and seen["kwargs"]["collector_token"] is None
    assert seen["batch"].coverage == "pending_updates_only" and run.extras["accepted"]
