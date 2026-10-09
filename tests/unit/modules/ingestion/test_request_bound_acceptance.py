"""Request-bound (bearer-less) collection authority and in-transaction settlement hooks."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from modules.connectors import public as connectors
from modules.connectors.collection_schemas import CollectionRequestRef
from modules.ingestion import public as ingestion

REF = CollectionRequestRef(request_id=uuid4(), admission_token=uuid4())
SCOPE = SimpleNamespace(workspace_id=uuid4())


class NoDb:
    async def scalar(self, *_args, **_kwargs):
        return None


async def call(**overrides):
    args = {"source_id": uuid4(), "source_generation": 1, "connector_revision": 2,
            "collector_token": None, "request_ref": None, "scope": SCOPE}
    return await ingestion._authorize_collection(NoDb(), **{**args, **overrides})


@pytest.mark.asyncio
async def test_neither_bearer_nor_request_is_rejected():
    with pytest.raises(HTTPException) as exc:
        await call()
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_unknown_bearer_is_rejected_without_a_request():
    with pytest.raises(HTTPException) as exc:
        await call(collector_token="not-a-credential")
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_request_ref_must_prove_the_exact_attempt(monkeypatch):
    seen = {}

    async def proof(session, ref, **kwargs):
        seen.update(kwargs, ref=ref)
        return seen.get("ok", False)

    monkeypatch.setattr(connectors, "lock_collection_request_in_uow", proof)
    with pytest.raises(HTTPException) as exc:
        await call(request_ref=REF)
    assert exc.value.status_code == 409
    assert seen["source_generation"] == 1 and seen["connector_revision"] == 2 and seen["ref"] == REF

    async def good(*_a, **_k):
        return True

    monkeypatch.setattr(connectors, "lock_collection_request_in_uow", good)
    await call(request_ref=REF)  # no bearer needed


@pytest.mark.asyncio
async def test_request_ref_without_connector_revision_fails_closed(monkeypatch):
    async def good(*_a, **_k):
        return True

    monkeypatch.setattr(connectors, "lock_collection_request_in_uow", good)
    with pytest.raises(HTTPException):
        await call(request_ref=REF, connector_revision=None)


@pytest.mark.asyncio
async def test_settle_is_noop_without_ref_and_409_when_attempt_is_stale(monkeypatch):
    ids = {"scope": SCOPE, "source_id": uuid4(), "source_generation": 1, "connector_revision": 2}
    await ingestion._settle_request(None, None, outcome="succeeded", run_id=None, **ids)
    calls = []

    async def stale(session, ref, **kwargs):
        calls.append(kwargs)
        return False

    monkeypatch.setattr(connectors, "settle_collection_in_uow", stale)
    with pytest.raises(HTTPException) as exc:
        await ingestion._settle_request(RecSession(), REF, outcome="no_changes", run_id=None, **ids)
    assert exc.value.status_code == 409 and calls[0]["outcome"] == "no_changes" and calls[0]["accepted_receipt_id"]


# ---------------------------------------------------------------- C3b receipts and retention

class RecSession:
    def __init__(self, existing=None, rows=None):
        self.existing, self.rows, self.added, self.executed = existing, rows, [], []

    async def scalar(self, *_a, **_k):
        return self.existing

    def add(self, row):
        self.added.append(row)

    async def flush(self):
        return None

    async def execute(self, stmt):
        self.executed.append(stmt)
        return SimpleNamespace(all=lambda: self.rows)


async def _settle(session, monkeypatch, **kwargs):
    seen = {}

    async def settle(_session, ref, **kw):
        seen.update(kw)
        return True

    monkeypatch.setattr(connectors, "settle_collection_in_uow", settle)
    await ingestion._settle_request(
        session, REF, outcome=kwargs.pop("outcome", "no_changes"), run_id=None, scope=SCOPE,
        source_id=uuid4(), source_generation=3, connector_revision=2, **kwargs)
    return seen


@pytest.mark.asyncio
async def test_acceptance_writes_a_receipt_and_links_the_request_in_the_same_flush(monkeypatch):
    from modules.ingestion.schemas import CollectionStateUpdate

    session = RecSession()
    seen = await _settle(session, monkeypatch, state_update=CollectionStateUpdate(
        update_validators=True, etag='"e"', coverage="complete"), cursor_before="a", cursor_after="b")
    (receipt,) = session.added
    assert receipt.request_id == REF.request_id and receipt.outcome == "no_changes" and receipt.etag == '"e"'
    assert receipt.cursor_before == "a" and receipt.cursor_after == "b" and receipt.retain_until > receipt.accepted_at
    assert seen["accepted_receipt_id"] == receipt.id


@pytest.mark.asyncio
async def test_replay_reuses_the_existing_receipt_and_never_inserts_a_second(monkeypatch):
    existing = uuid4()
    session = RecSession(existing=existing)
    seen = await _settle(session, monkeypatch)
    assert session.added == [] and seen["accepted_receipt_id"] == existing


@pytest.mark.asyncio
async def test_cleanup_keeps_receipts_whose_request_is_still_recoverable(monkeypatch):
    live, done = uuid4(), uuid4()
    rows = [SimpleNamespace(id=uuid4(), request_id=live), SimpleNamespace(id=uuid4(), request_id=done)]
    session = RecSession(rows=rows)

    async def recoverable(_session, ids):
        assert set(ids) == {live, done}
        return {live}

    monkeypatch.setattr(connectors, "recoverable_collection_request_ids", recoverable)
    assert await ingestion.purge_expired_collection_receipts(session) == 1
    assert len(session.executed) == 2  # candidate select, then one delete for the unrecoverable receipt only
    assert rows[1].id in session.executed[1].compile().params["id_1"]
    assert rows[0].id not in session.executed[1].compile().params["id_1"]
