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
async def test_unsupported_providers_fail_closed_without_a_send(monkeypatch):
    async def adapter(run):
        raise AssertionError("must not run")

    for provider, adapter_fn in (("web", None),):
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
    class _Empty:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def get(self, *_args):
            return None  # no owner-entered credential stored

        async def rollback(self):
            return None

    run = SimpleNamespace(attempt=attempt(authenticated=True), factory=_Empty, gate=SimpleNamespace(fetch_bytes=None))
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
    assert len(out["records"]) == 500 and stats == {"truncated": True}  # mid-page: no exact continuation

    async def fetch_pages(url):
        return feed(250, "https://a.example/feed?p=2") if "p=2" not in url else feed(250, "https://a.example/feed?p=3")

    stats = {}
    out = await n8n.read_rss("https://a.example/feed", None, fetch=fetch_pages, stats=stats)
    assert len(out["records"]) == 500 and stats == {"resume_url": "https://a.example/feed?p=3"}

    async def fetch_small(url):
        return feed(3)

    stats = {}
    out = await n8n.read_rss("https://a.example/feed", None, fetch=fetch_small, stats=stats)
    assert len(out["records"]) == 3 and stats == {}


# ---------------------------------------------------------------- C3b: continuation, validators, 304

def _rec(i):
    return {"provider_id": str(i), "content": "c", "observed_at": datetime.now(UTC).isoformat(), "version": None, "metadata": {}}


def _capture(monkeypatch):
    got = {}

    async def accept(run, records, before, after, update=None):
        got.update(records=records, before=before, after=after, update=update)

    monkeypatch.setattr(collection, "_accept_generic", accept)
    return got


@pytest.mark.asyncio
async def test_partial_walk_commits_a_checkpoint_and_leaves_the_cursor(monkeypatch):
    got = _capture(monkeypatch)
    state = collection.CollectionState(cursor="2026-01-01T00:00:00+00:00")
    walk = collection.Walk([_rec(1), _rec(2)], "https://a.example/p3", "2026-02-01T00:00:00+00:00", '"e"', None)
    await collection._accept_walk(SimpleNamespace(attempt=attempt()), state, walk, None)
    assert got["before"] == got["after"] == state.cursor  # no skip: cursor only moves when the walk is complete
    update = got["update"]
    assert update.coverage == "partial" and not update.update_validators
    cp = rest.Checkpoint.decode(update.continuation_state, revision=2, cursor=state.cursor, origin_url=None)
    assert cp.url == "https://a.example/p3" and cp.max_time == "2026-02-01T00:00:00+00:00"
    assert cp.ids == [rest.id_hash("1"), rest.id_hash("2")]


@pytest.mark.asyncio
async def test_complete_walk_advances_cursor_clears_checkpoint_and_stores_validators(monkeypatch):
    got = _capture(monkeypatch)
    state = collection.CollectionState(cursor="2026-01-01T00:00:00+00:00")
    walk = collection.Walk([_rec(1)], None, "2026-02-01T00:00:00+00:00", '"e2"', "lm")
    await collection._accept_walk(SimpleNamespace(attempt=attempt()), state, walk, None)
    update = got["update"]
    assert got["after"] == "2026-02-01T00:00:00+00:00" and update.coverage == "complete"
    assert update.continuation_state is None and update.update_validators and update.etag == '"e2"'


@pytest.mark.asyncio
async def test_final_empty_segment_still_earns_the_cursor_through_the_no_change_path(monkeypatch):
    got = _capture(monkeypatch)
    state = collection.CollectionState(cursor="2026-01-01T00:00:00+00:00")
    walk = collection.Walk([], None, "2026-02-01T00:00:00+00:00", None, None)
    await collection._accept_walk(SimpleNamespace(attempt=attempt()), state, walk, None)
    assert got["records"] == [] and got["update"].cursor_after == "2026-02-01T00:00:00+00:00"


@pytest.mark.asyncio
async def test_not_modified_is_a_receipted_no_change_without_cursor_or_validator_changes(monkeypatch):
    got = _capture(monkeypatch)
    state = collection.CollectionState(cursor="c", etag='"e"', validators_revision=2)
    await collection._accept_walk(SimpleNamespace(attempt=attempt()), state, collection.Walk([], None, None, None, None, True), None)
    update = got["update"]
    assert got["records"] == [] and got["after"] == "c"
    assert not update.update_validators and update.cursor_after is None and update.continuation_state is None


def test_conditional_headers_need_validators_stored_under_the_current_revision():
    state = collection.CollectionState(etag='"e"', last_modified="lm", validators_revision=2)
    assert collection._conditional(state, 2) == {"If-None-Match": '"e"', "If-Modified-Since": "lm"}
    assert collection._conditional(state, 3) is None  # reconfigured: validators are not trusted
    assert collection._conditional(collection.CollectionState(), 2) is None


def _rest_harness(monkeypatch, state, pages):
    sent = []
    got = _capture(monkeypatch)

    async def get_state(run):
        return state

    async def fetch(url, **kwargs):
        sent.append((url, kwargs.get("headers")))
        return pages.pop(0)

    monkeypatch.setattr(collection, "_state", get_state)
    from modules.connectors import registry

    monkeypatch.setattr(registry, "configuration", lambda source: SimpleNamespace(
        url="https://a.example/items", feed_url="https://a.example/feed", items_path="items", id_field="id",
        title_field=None, content_field=None, updated_field=None))
    run = SimpleNamespace(attempt=attempt(), gate=SimpleNamespace(fetch_bytes=fetch))
    return run, sent, got


@pytest.mark.asyncio
async def test_rest_sends_stored_validators_and_a_304_settles_no_change(monkeypatch):
    state = collection.CollectionState(cursor="c", etag='"e"', validators_revision=2)
    run, sent, got = _rest_harness(monkeypatch, state, [rest.Fetched(None)])
    await collection._run_rest(run)
    assert sent == [("https://a.example/items", {"If-None-Match": '"e"'})]
    assert got["records"] == [] and got["update"].update_validators is False


@pytest.mark.asyncio
async def test_rest_resumes_from_checkpoint_without_conditional_headers(monkeypatch):
    cp = rest.Checkpoint(url="https://a.example/items?p=2", revision=2, cursor="c", ids=[rest.id_hash("1")])
    state = collection.CollectionState(cursor="c", etag='"e"', validators_revision=2, continuation_state=cp.encode())
    body = rest.Fetched(b'{"items": [{"id": 1}, {"id": 2}]}')
    run, sent, got = _rest_harness(monkeypatch, state, [body])
    await collection._run_rest(run)
    assert sent == [("https://a.example/items?p=2", None)]
    assert [r["provider_id"] for r in got["records"]] == ["2"]  # boundary record 1 not delivered twice
