"""Unit tests for the shared collection service with fakes (no database, no network)."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from modules.connectors import collection, n8n, quota
from modules.connectors import public as connectors
from modules.connectors.collection_schemas import CollectionAdmissionRead, CollectionRequestRef
from modules.connectors.providers import rest

REF = CollectionRequestRef(request_id=uuid4(), admission_token=uuid4())


class FakeSession:
    def __init__(self, log):
        self.log = log

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def commit(self):
        self.log.append("commit")

    async def rollback(self):
        self.log.append("rollback")


def factory(log):
    return lambda: FakeSession(log)


def attempt(provider=None, source_type="api", authenticated=False, terms=None):
    source = SimpleNamespace(id=uuid4(), provider=provider, type=source_type, generation=1, configuration={},
                             workspace_id=uuid4())
    return collection.Attempt(
        ref=REF, attempt=1, scope=SimpleNamespace(workspace_id=uuid4()), multi=True, source=source,
        access_fence=None, source_fence=None, terms_revision=terms, connector_revision=2,
        authenticated=authenticated)


def test_snapshot_matches_requires_all_three_to_agree():
    assert collection.snapshot_matches((1, 2, 3), (1, 2, 3))
    assert not collection.snapshot_matches((1, 2, 3), (1, 3, 3))
    assert not collection.snapshot_matches((1, 2, 3), (2, 2, 3))
    assert not collection.snapshot_matches((1, 2, 3), (1, 2, 4))


@pytest.mark.parametrize("exc, code, retryable", [
    (rest.CollectionIncomplete(), "collection_incomplete", None),
    (rest.RestSchemaChanged("x"), "schema_changed", None),
    (collection.TermsIneligible(), "terms_not_accepted", None),
    (rest.UnsafeDestination("x"), "destination_blocked", None),
    (rest.ProviderHttpError(401), "invalid_credential", None),
    (rest.ProviderHttpError(503), "provider_unavailable", True),
    (rest.ProviderHttpError(404), "provider_rejected", False),
    (TimeoutError(), "provider_timeout", True),
    (HTTPException(status_code=409), "ingestion_conflict", True),
    (ValueError("x"), "provider_response_invalid", None),
    (RuntimeError("boom"), "executor_error", True),
])
def test_failures_map_to_bounded_codes(exc, code, retryable):
    got = collection._classify(exc)
    assert got["error_code"] == code and got.get("retryable") == retryable


@pytest.mark.asyncio
async def test_gate_debits_once_per_physical_send_and_rechecks_at_header_time(monkeypatch):
    log, debits = [], []

    async def authorize(self):
        log.append("authorize")

    async def debit_send(session, **kwargs):
        debits.append(kwargs["send_sequence"])

    monkeypatch.setattr(collection.SendGate, "_authorize", authorize)
    monkeypatch.setattr(quota, "debit_send", debit_send)
    gate = collection.SendGate(factory(log), attempt("open_meteo"), settings=None, deadline=1e18, quota_provider="open_meteo")
    for _ in range(2):  # two sends, each announced by a pre-call and a header-time call
        await gate()
        await gate()
    assert debits == [1, 2]
    assert log.count("authorize") == 4
    assert log.index("commit") < len(log)  # the debit is committed (before the wire send)


@pytest.mark.asyncio
async def test_gate_exhausted_budget_raises_before_any_send_and_commits_nothing(monkeypatch):
    log = []

    async def authorize(self):
        return None

    async def debit_send(session, **kwargs):
        raise quota.QuotaDeferred("open_meteo:day", datetime.now(UTC) + timedelta(hours=1))

    monkeypatch.setattr(collection.SendGate, "_authorize", authorize)
    monkeypatch.setattr(quota, "debit_send", debit_send)
    gate = collection.SendGate(factory(log), attempt("open_meteo"), settings=None, deadline=1e18, quota_provider="open_meteo")
    with pytest.raises(quota.QuotaDeferred):
        await gate()
    assert "commit" not in log and "rollback" in log


@pytest.mark.asyncio
async def test_gate_rejects_a_changed_alpha_credential_operation(monkeypatch):
    async def authorize(self):
        return None

    monkeypatch.setattr(collection.SendGate, "_authorize", authorize)
    gate = collection.SendGate(factory([]), attempt("alpha_vantage"), settings=None, deadline=1e18, quota_provider=None)
    first = uuid4()
    await gate(first)
    await gate(first)  # header-time recheck of the same send
    with pytest.raises(collection.CollectionFenceLost):
        await gate(uuid4())


@pytest.mark.asyncio
async def test_gate_past_deadline_is_a_timeout_not_a_send():
    gate = collection.SendGate(factory([]), attempt(), settings=None, deadline=0.0, quota_provider=None)
    with pytest.raises(TimeoutError):
        await gate()


def harness(monkeypatch, adapter, att=None, settled=None):
    settled = settled if settled is not None else []

    async def load(factory_, admission, multi):
        return att or attempt()

    async def settle(factory_, admission, **kwargs):
        settled.append(kwargs)
        return True

    async def noop(*_a, **_k):
        return None

    monkeypatch.setattr(collection, "_load_attempt", load)
    monkeypatch.setattr(collection, "_settle", settle)
    monkeypatch.setattr(collection, "_release_lease", noop)
    monkeypatch.setattr(collection, "_record_rate_limit", noop)
    monkeypatch.setattr(collection, "_adapter_for", lambda source: adapter)
    ctx = {"settings": SimpleNamespace(multi_workspace_enabled=True), "session_factory": factory([])}
    admission = CollectionAdmissionRead(request_id=REF.request_id, source_id=uuid4(), admission_token=REF.admission_token, attempt=1)
    return ctx, admission, settled


@pytest.mark.asyncio
async def test_accepted_run_leaves_settlement_to_the_ingestion_commit(monkeypatch):
    async def adapter(run):
        return None

    ctx, admission, settled = harness(monkeypatch, adapter)
    await collection.execute_collection(ctx, admission)
    assert settled == []


@pytest.mark.asyncio
async def test_quota_deferral_defers_without_burning_the_attempt(monkeypatch):
    until = datetime.now(UTC) + timedelta(hours=2)

    async def adapter(run):
        raise quota.QuotaDeferred("p", until)

    ctx, admission, settled = harness(monkeypatch, adapter)
    await collection.execute_collection(ctx, admission)
    assert settled == [{"outcome": "deferred", "provider_deadline": until}]


@pytest.mark.asyncio
async def test_provider_429_keeps_the_full_retry_after(monkeypatch):
    until = datetime.now(UTC) + timedelta(days=2)

    async def adapter(run):
        raise connectors.ProviderRateLimited(until)

    ctx, admission, settled = harness(monkeypatch, adapter)
    await collection.execute_collection(ctx, admission)
    assert settled[0]["provider_deadline"] == until and settled[0]["retryable"] and settled[0]["outcome"] == "failed"


@pytest.mark.asyncio
async def test_lost_authority_publishes_nothing_and_cancels_only_when_asked(monkeypatch):
    async def lost(run):
        raise collection.CollectionFenceLost("admission_lost")

    ctx, admission, settled = harness(monkeypatch, lost)
    await collection.execute_collection(ctx, admission)
    assert settled == []  # C2 recovery owns an expired slot

    async def changed(run):
        raise collection.CollectionFenceLost("terms_changed", "revision_changed")

    ctx, admission, settled = harness(monkeypatch, changed)
    await collection.execute_collection(ctx, admission)
    assert settled == [{"outcome": "cancelled", "error_code": "revision_changed"}]


@pytest.mark.asyncio
async def test_unsupported_and_proof_bearing_providers_fail_closed_without_a_send(monkeypatch):
    async def adapter(run):
        raise AssertionError("must not run")

    for provider, adapter_fn in (("github", adapter), ("telegram", adapter), ("web", None)):
        ctx, admission, settled = harness(monkeypatch, adapter_fn, attempt(provider if provider != "web" else None, "web"))
        await collection.execute_collection(ctx, admission)
        assert settled == [{"outcome": "failed", "error_code": "collection_unsupported"}]


@pytest.mark.asyncio
async def test_ninety_second_run_deadline_is_a_retryable_timeout(monkeypatch):
    monkeypatch.setattr(rest, "RUN_DEADLINE_SECONDS", 0.05)

    async def slow(run):
        await asyncio.sleep(5)

    ctx, admission, settled = harness(monkeypatch, slow)
    await collection.execute_collection(ctx, admission)
    assert settled[0]["error_code"] == "provider_timeout" and settled[0]["retryable"]


@pytest.mark.asyncio
async def test_authenticated_native_rest_requires_credential_reentry(monkeypatch):
    run = SimpleNamespace(attempt=attempt(authenticated=True))
    with pytest.raises(rest.ProviderHttpError) as exc:
        await collection._run_rest(run)
    assert collection._classify(exc.value)["error_code"] == "invalid_credential"


@pytest.mark.asyncio
async def test_generic_accept_uses_request_ref_and_a_request_scoped_batch_key(monkeypatch):
    calls = {}

    async def lock(*_a, **_k):
        return None

    async def receive(session, batch, token, **kwargs):
        calls["receive"] = (batch, token, kwargs)

    async def no_changes(session, **kwargs):
        calls["none"] = kwargs

    monkeypatch.setattr(collection, "lock_access_fence", lock)
    monkeypatch.setattr(collection.ingestion, "receive_connector_batch", receive)
    monkeypatch.setattr(collection.ingestion, "accept_collection_no_changes", no_changes)
    run = SimpleNamespace(attempt=attempt(), factory=factory([]))
    record = {"provider_id": "1", "content": "c", "observed_at": datetime.now(UTC).isoformat(), "version": None, "metadata": {}}
    await collection._accept_generic(run, [record], None, "2026-01-01T00:00:00+00:00")
    batch, token, kwargs = calls["receive"]
    assert token is None and kwargs["request_ref"] == REF
    assert batch.batch_key == f"connector-request:{REF.request_id}" and batch.connector_revision == 2
    await collection._accept_generic(run, [], None, None)
    assert calls["none"]["request_ref"] == REF  # empty: no batch, no cursor, no fake data


@pytest.mark.asyncio
async def test_rss_truncation_is_reported_so_the_cursor_cannot_advance():
    def feed(n, nxt=None):
        items = "".join(
            f"<item><guid>g{i}</guid><title>t{i}</title><pubDate>2026-01-01T00:00:00Z</pubDate></item>" for i in range(n))
        link = f'<link rel="next" href="{nxt}"/>' if nxt else ""
        return f"<rss><channel>{link}{items}</channel></rss>".encode()

    async def fetch_many(url):
        return feed(600)

    stats: dict[str, bool] = {}
    out = await n8n.read_rss("https://a.example/feed", None, fetch=fetch_many, stats=stats)
    assert len(out["records"]) == 500 and stats == {"truncated": True}

    async def fetch_small(url):
        return feed(3)

    stats = {}
    out = await n8n.read_rss("https://a.example/feed", None, fetch=fetch_small, stats=stats)
    assert len(out["records"]) == 3 and stats == {}
