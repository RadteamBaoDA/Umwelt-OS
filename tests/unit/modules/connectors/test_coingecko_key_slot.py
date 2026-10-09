"""CoinGecko owner key slot: header-only, fail-closed, fenced, never echoed (fakes only; no DB, no network)."""

import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException

from modules.connectors import collection
from modules.connectors.credentials import decrypt_rest_secret
from modules.connectors.providers import rest
from modules.connectors.providers.crypto import COINGECKO_KEY_HEADER, coingecko_request

KEY = "CG-demo-key-1234567890"
SOURCE_ID = uuid4()
DEPLOY = Fernet.generate_key().decode("ascii")
BODY = json.dumps({"bitcoin": {"usd": 65000.5}}).encode()


class Gate:
    def __init__(self):
        self.calls, self.fingerprint = [], None

    def bind_credential(self, fingerprint):
        self.fingerprint = fingerprint

    async def fetch_bytes(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return rest.Fetched(BODY)


def make_run(monkeypatch, secret):
    accepted, leased = [], []

    async def lease(_run):
        leased.append(1)
        return object()

    async def accept(_run, _lease, records, coverage, _at, update=None):
        accepted.append(records)

    async def stored(_run):
        if isinstance(secret, Exception):
            raise secret
        return secret

    monkeypatch.setattr(collection, "_lease", lease)
    monkeypatch.setattr(collection, "_accept_native", accept)
    monkeypatch.setattr(collection, "_rest_secret", stored)
    settings = SimpleNamespace(connector_credential_encryption_key=SimpleNamespace(get_secret_value=lambda: DEPLOY))
    run = SimpleNamespace(
        attempt=SimpleNamespace(source=SimpleNamespace(provider="coingecko")), gate=Gate(), settings=settings, extras={})
    return run, accepted, leased


def test_registered_and_dispatchable():
    from modules.connectors.provider_specs import DISPATCHABLE, get_provider_spec

    assert "coingecko" in DISPATCHABLE and "coingecko" in collection.ADAPTERS
    spec = get_provider_spec("coingecko")
    assert spec is not None and spec.code_implemented and not spec.runtime_verified and spec.key_required


async def test_key_rides_the_header_only_and_never_leaks(monkeypatch, caplog):
    run, accepted, _ = make_run(monkeypatch, (COINGECKO_KEY_HEADER, KEY))
    with caplog.at_level(logging.DEBUG):
        await collection._run_coingecko(run)
    (url, kwargs), = run.gate.calls
    assert KEY not in url and kwargs["headers"] == {COINGECKO_KEY_HEADER: KEY}
    assert KEY not in repr(coingecko_request(KEY))
    assert KEY not in caplog.text and KEY not in repr(accepted)
    assert KEY not in json.dumps([r.model_dump(mode="json") for r in accepted[0]])
    assert run.gate.fingerprint and KEY not in run.gate.fingerprint  # shared budget keys on an HMAC, not the key


@pytest.mark.parametrize("secret", [
    rest.ProviderHttpError(401),  # no row, stale generation/revision, revoked or undecryptable
    ("x-other-header", KEY),
])
async def test_missing_key_fails_closed_before_lease_or_send(monkeypatch, secret):
    run, accepted, leased = make_run(monkeypatch, secret)
    with pytest.raises(collection.CredentialMissing) as caught:
        await collection._run_coingecko(run)
    assert not run.gate.calls and not leased and not accepted
    assert KEY not in str(caught.value)
    assert collection._classify(caught.value) == {"outcome": "failed", "error_code": "credential_missing"}


def test_credential_missing_gates_the_schedule_until_the_key_is_reentered():
    from modules.connectors.scheduler import ACTION_REQUIRED

    assert ACTION_REQUIRED["credential_missing"] == "credential"


@pytest.mark.parametrize("current, lost", [(5, False), (6, True)])
async def test_credential_revision_change_fences_in_flight_sends(current, lost):
    attempt = SimpleNamespace(
        scope=object(), multi=True, access_fence=object(), credential_revision=5)
    gate = collection.SendGate(
        None, attempt, settings=SimpleNamespace(), deadline=0.0, quota_provider="coingecko")  # type: ignore[arg-type]
    row = SimpleNamespace(credential_revision=current)
    session = SimpleNamespace(rollback=AsyncMock())
    with patch.object(collection.provisioning, "lock_connector", AsyncMock(return_value=(object(), row, {}))):
        if lost:
            with pytest.raises(collection.CollectionFenceLost) as caught:
                await gate._check_credential_revision(session, SimpleNamespace(id=SOURCE_ID))
            assert caught.value.cancel_as == "revision_changed"
        else:
            await gate._check_credential_revision(session, SimpleNamespace(id=SOURCE_ID))
    session.rollback.assert_awaited_once()


def _reentry(provider_secret, row_over=None):
    from modules.connectors import provisioning_routes as routes

    r = SimpleNamespace(
        source_id=SOURCE_ID, desired_revision=3, credential_revision=4, state="active", desired_configuration={}, **(row_over or {}))
    source = SimpleNamespace(id=SOURCE_ID, status="active", type="api", provider="coingecko", generation=1)
    stored = []
    session = SimpleNamespace(rollback=AsyncMock(), add=stored.append, get=AsyncMock(return_value=None))
    settings = SimpleNamespace(
        multi_workspace_enabled=True, connector_credential_encryption_key=SimpleNamespace(get_secret_value=lambda: DEPLOY))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=settings)))
    payload = routes.NativeRestCredentialRequest(expected_revision=3, secret=provider_secret)
    patches = (
        patch.object(routes, "_owner_access", AsyncMock(return_value=object())),
        patch.object(routes, "_source", AsyncMock(return_value=source)),
        patch.object(routes.provisioning, "activation_status", AsyncMock(return_value=r)),
        patch.object(routes.provisioning, "lock_connector",
                     AsyncMock(return_value=(SimpleNamespace(status="active", generation=1), r, {}))),
        patch.object(routes.scheduler, "clear_collection_block", AsyncMock(return_value=True)),
        patch.object(routes, "commit_with_replay", AsyncMock()),
        patch.object(routes, "make_source_change", lambda *a, **k: None),
        patch.object(routes, "_activation_read", AsyncMock(return_value="read")),
    )
    return routes, r, stored, session, request, payload, patches


async def test_reentry_stores_encrypted_key_in_fixed_header_slot_and_bumps_revision():
    routes, r, stored, session, request, payload, patches = _reentry(KEY)
    for p in patches:
        p.start()
    try:
        result = await routes.reenter_native_rest_credential(
            SOURCE_ID, payload, session, request, SimpleNamespace(workspace_id=uuid4()))
    finally:
        for p in patches:
            p.stop()
    assert result == "read" and r.credential_revision == 5
    (credential,) = stored
    assert credential.header_name == COINGECKO_KEY_HEADER and credential.state == "ready"
    assert KEY not in credential.encrypted_secret and KEY not in (credential.secret_fingerprint or "")
    assert decrypt_rest_secret(
        DEPLOY, credential.encrypted_secret, source_id=SOURCE_ID, operation_id=credential.operation_id,
        source_generation=1, configuration_revision=3, header_name=COINGECKO_KEY_HEADER) == KEY
    assert KEY not in repr(payload) and KEY not in payload.model_dump_json()  # SecretStr masks the request model


async def test_reentry_rejects_unusable_key_without_echoing_it():
    bad = "k" * 300  # passes the generic 512-byte rule, exceeds the CoinGecko adapter bound
    routes, _r, stored, session, request, payload, patches = _reentry(bad)
    for p in patches:
        p.start()
    try:
        with pytest.raises(HTTPException) as caught:
            await routes.reenter_native_rest_credential(
                SOURCE_ID, payload, session, request, SimpleNamespace(workspace_id=uuid4()))
    finally:
        for p in patches:
            p.stop()
    assert caught.value.status_code == 422 and bad not in str(caught.value.detail) and not stored


async def test_activation_requires_a_ready_key_for_coingecko():
    from modules.connectors import activation

    source = SimpleNamespace(
        id=SOURCE_ID, status="active", provider="coingecko", generation=1, type="api", configuration={})
    row = SimpleNamespace(
        source_generation=1, workflow_operation=None, activation_intent=None, desired_revision=3,
        desired_configuration={})
    session = SimpleNamespace(get=AsyncMock(return_value=None))
    with (
        patch.object(activation.sources, "get_connector_source", AsyncMock(return_value=source)),
        patch.object(activation.provider_terms, "require_terms_eligible", AsyncMock(return_value=1)),
        patch("modules.settings.public.module_is_enabled", AsyncMock(return_value=True)),
    ):
        out = await activation.activate_native_in_uow(
            session, SOURCE_ID, SimpleNamespace(local_only=False), row, scope=object(), multi_workspace_enabled=False)
    assert out == "invalid_credential"
