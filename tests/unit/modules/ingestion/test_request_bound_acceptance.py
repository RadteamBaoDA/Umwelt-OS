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
    await ingestion._settle_request(None, None, outcome="succeeded", run_id=None)
    calls = []

    async def stale(session, ref, **kwargs):
        calls.append(kwargs)
        return False

    monkeypatch.setattr(connectors, "settle_collection_in_uow", stale)
    with pytest.raises(HTTPException) as exc:
        await ingestion._settle_request(None, REF, outcome="no_changes", run_id=None)
    assert exc.value.status_code == 409 and calls == [{"outcome": "no_changes", "ingestion_run_id": None}]
