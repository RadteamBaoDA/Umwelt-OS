"""C4: native activation, backend transition saga, readiness fence, REST credential and n8n envelopes (fakes only)."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException

from modules.connectors import activation, backends, provisioning
from modules.connectors.credentials import CredentialEncryptionUnavailable, decrypt_rest_secret, encrypt_rest_secret
from modules.connectors.n8n import build_workflow

SOURCE_ID = uuid4()
KEY = Fernet.generate_key().decode("ascii")


def row(**over):
    base = dict(
        source_id=SOURCE_ID, source_generation=1, desired_revision=3, applied_revision=3, desired_enabled=True,
        state="active", error_code=None, execution_backend="n8n", backend_revision=1, applied_backend_revision=1,
        transition_phase="idle", target_backend=None, transition_operation_id=None, old_workflow_id=None,
        workflow_id="wf1", workflow_name="n", workflow_operation=None, activation_intent=None,
        template_revision=1, applied_template_revision=1, credential_revision=1, desired_configuration={})
    base.update(over)
    return SimpleNamespace(**base)


# --------------------------------------------------------------------------- readiness

@pytest.mark.parametrize("changes,expected", [
    ({}, True),
    ({"execution_backend": "native", "workflow_id": None, "applied_template_revision": 0}, True),  # no workflow needed
    ({"workflow_id": None}, False),  # n8n needs a confirmed workflow
    ({"applied_template_revision": 0}, False),  # old token-only template fails closed
    ({"transition_phase": "draining"}, False),
    ({"transition_phase": "reconciliation_required"}, False),
    ({"backend_revision": 2}, False),  # revision invalidated, applied one is stale
    ({"execution_backend": "native", "backend_revision": 2}, False),
])
def test_backend_admits(changes, expected):
    assert backends.backend_admits(row(**changes)) is expected


# --------------------------------------------------------------------------- transition begin

class _Locks:
    """Patch lock_connector + request fencing so begin runs without a database."""

    def __init__(self, r, slots=None, status="active"):
        self.r, self.slots = r, slots or {}
        self.fence = SimpleNamespace(status=status, generation=1)

    def __enter__(self):
        self.p = [
            patch.object(provisioning, "lock_connector", AsyncMock(return_value=(self.fence, self.r, self.slots))),
            patch.object(provisioning, "fence_active_requests", AsyncMock()),
        ]
        self.fenced = self.p[1].start()
        self.p[0].start()
        return self

    def __exit__(self, *exc):
        for p in self.p:
            p.stop()


async def begin(r, target="native", revision=3, **kw):
    session = SimpleNamespace(flush=AsyncMock())
    return await provisioning.begin_backend_transition_in_uow(
        session, SOURCE_ID, revision, target, scope=object(), multi_workspace_enabled=True, access_fence=object())


@pytest.mark.asyncio
async def test_begin_invalidates_revision_stops_admission_and_fences_work():
    r = row()
    with _Locks(r) as locks:
        await begin(r)
    assert (r.backend_revision, r.transition_phase, r.target_backend) == (2, "draining", "native")
    assert r.old_workflow_id == "wf1" and r.desired_enabled is False and r.state == "saved_not_active"
    assert not backends.backend_admits(r)  # stopped before anything else happens
    locks.fenced.assert_awaited_once()


@pytest.mark.asyncio
async def test_begin_from_native_has_no_old_workflow_to_stop():
    r = row(execution_backend="native", workflow_id="old-inactive")
    with _Locks(r):
        await begin(r, target="n8n")
    assert r.old_workflow_id is None and r.backend_revision == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("r,target,revision,code", [
    (row(), "native", 9, 409),  # stale revision
    (row(transition_phase="draining"), "native", 3, 409),  # already transitioning
    (row(), "n8n", 3, 409),  # same backend and current template: nothing to do
    (row(workflow_operation={"id": "x"}), "native", 3, 409),  # pending workflow operation
    (row(state="provisioning"), "native", 3, 409),
])
async def test_begin_rejects(r, target, revision, code):
    with _Locks(r), pytest.raises(HTTPException) as caught:
        await begin(r, target=target, revision=revision)
    assert caught.value.status_code == code and r.backend_revision == 1


@pytest.mark.asyncio
async def test_begin_allows_template_upgrade_for_stale_n8n_template():
    r = row(applied_template_revision=0)
    with _Locks(r):
        await begin(r, target="n8n")
    assert r.transition_phase == "draining" and r.target_backend == "n8n"


@pytest.mark.asyncio
async def test_begin_rejects_unresolved_credential_slot_and_inactive_source():
    r = row()
    with _Locks(r, slots={"collector": SimpleNamespace(state="reconciliation_required")}), pytest.raises(HTTPException):
        await begin(r)
    with _Locks(r, status="paused"), pytest.raises(HTTPException):
        await begin(r)


# --------------------------------------------------------------------------- transition advance

class _Advance:
    def __init__(self, r):
        self.r = r

    def __enter__(self):
        fence = SimpleNamespace(status="active", generation=1, local_only=False)
        self.p = [
            patch.object(provisioning, "lock_connector", AsyncMock(return_value=(fence, self.r, {}))),
            patch.object(provisioning, "_connector_observation", lambda *a, **k: None),
            patch.object(provisioning, "commit_connector_observation", AsyncMock()),
            patch.object(provisioning, "fence_active_requests", AsyncMock()),
            patch.object(provisioning, "_new_deactivation", lambda r, g, **k: {"kind": "deactivate", "step": {"state": "prepared"}}),
            patch.object(provisioning, "drive_workflow_operation", AsyncMock(return_value=True)),
            patch.object(activation, "activate_native_in_uow", AsyncMock(return_value="")),
        ]
        self.m = [p.start() for p in self.p]
        return self

    def __exit__(self, *exc):
        for p in self.p:
            p.stop()


async def advance(r, api=object()):
    session = SimpleNamespace(rollback=AsyncMock())
    return await provisioning.advance_backend_transition(
        session, SOURCE_ID, api, scope=object(), multi_workspace_enabled=True, access_fence=object())


@pytest.mark.asyncio
async def test_draining_stops_old_workflow_before_touching_the_new_backend():
    r = row(transition_phase="draining", target_backend="native", old_workflow_id="wf1", backend_revision=2)
    order = []
    with _Advance(r) as a:
        async def drive(*args, **kwargs):
            order.append("stop-old")
            r.workflow_operation = None  # the reviewed saga acknowledged the deactivation
            return True

        async def activate_new(*args, **kwargs):
            order.append("activate-new")
            return ""

        a.m[5].side_effect, a.m[6].side_effect = drive, activate_new
        await advance(r)
    assert order == ["stop-old", "activate-new"]


@pytest.mark.asyncio
async def test_unacknowledged_stop_never_activates_the_new_backend():
    r = row(transition_phase="draining", target_backend="native", old_workflow_id="wf1", backend_revision=2)
    with _Advance(r) as a:
        a.m[5].return_value = False  # response lost: operation stays, nothing acknowledged
        await advance(r)
    assert r.workflow_operation["kind"] == "deactivate" and r.transition_phase == "deactivating_old"
    a.m[6].assert_not_awaited()
    assert not backends.backend_admits(r)


@pytest.mark.asyncio
async def test_acknowledged_stop_activates_native_and_never_before():
    r = row(transition_phase="deactivating_old", target_backend="native", old_workflow_id="wf1",
            backend_revision=2, workflow_operation=None)
    with _Advance(r) as a:
        phase = await advance(r)
    a.m[6].assert_awaited_once()
    assert r.execution_backend == "native" and phase == r.transition_phase


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["unknown", "dispatched", "rejected"])
async def test_ambiguous_stop_is_reconciliation_and_activates_nothing(state):
    r = row(transition_phase="deactivating_old", target_backend="native", old_workflow_id="wf1", backend_revision=2,
            workflow_operation={"kind": "deactivate", "step": {"state": state}})
    with _Advance(r) as a:
        phase = await advance(r)
    assert phase == "reconciliation_required" and r.error_code == "deactivation_unconfirmed"
    a.m[6].assert_not_awaited()
    a.m[5].assert_not_awaited()
    assert r.execution_backend == "n8n" and not backends.backend_admits(r)


@pytest.mark.asyncio
async def test_without_n8n_api_the_stop_stays_pending():
    r = row(transition_phase="deactivating_old", target_backend="native", old_workflow_id="wf1", backend_revision=2,
            workflow_operation={"kind": "deactivate", "step": {"state": "prepared"}})
    with _Advance(r) as a:
        phase = await advance(r, api=None)
    assert phase == "deactivating_old"
    a.m[5].assert_not_awaited()
    a.m[6].assert_not_awaited()


@pytest.mark.asyncio
async def test_n8n_target_waits_for_owner_activation():
    r = row(transition_phase="activating_new", target_backend="n8n", execution_backend="native", backend_revision=2)
    with _Advance(r) as a:
        phase = await advance(r)
    assert phase == "activating_new" and r.execution_backend == "n8n" and r.error_code == "activation_required"
    a.m[6].assert_not_awaited()
    assert not backends.backend_admits(r)


@pytest.mark.asyncio
async def test_failed_native_activation_keeps_transition_open_with_code():
    r = row(transition_phase="activating_new", target_backend="native", backend_revision=2)
    with _Advance(r) as a:
        a.m[6].return_value = "terms_not_accepted"
        phase = await advance(r)
    assert phase == "activating_new" and r.error_code == "terms_not_accepted" and not backends.backend_admits(r)


@pytest.mark.asyncio
async def test_resolve_retry_and_confirm():
    r = row(transition_phase="reconciliation_required", backend_revision=2, old_workflow_id="wf1")
    session = SimpleNamespace(flush=AsyncMock())
    kw = dict(scope=object(), multi_workspace_enabled=True, access_fence=object())
    with _Locks(r), patch.object(provisioning, "_new_deactivation", lambda *a, **k: {"kind": "deactivate"}):
        await provisioning.resolve_backend_transition_in_uow(session, SOURCE_ID, 3, "retry", **kw)
        assert r.transition_phase == "deactivating_old" and r.workflow_operation == {"kind": "deactivate"}
        r.transition_phase = "reconciliation_required"
        await provisioning.resolve_backend_transition_in_uow(session, SOURCE_ID, 3, "confirm_inactive", **kw)
        assert r.transition_phase == "activating_new" and r.workflow_operation is None
        with pytest.raises(HTTPException):  # nothing to resolve any more
            await provisioning.resolve_backend_transition_in_uow(session, SOURCE_ID, 3, "retry", **kw)


# --------------------------------------------------------------------------- native activation

class _NativeEnv:
    def __init__(self, r, *, terms=None, credential=None, supported=True, local_only=False, config=None):
        self.r, self.terms, self.credential, self.supported = r, terms, credential, supported
        self.fence = SimpleNamespace(local_only=local_only)
        self.source = SimpleNamespace(
            id=SOURCE_ID, status="active", generation=1, workspace_id=uuid4(), type="api", provider=None,
            configuration=config or {})

    def __enter__(self):
        from modules.connectors import collection

        self.p = [
            patch.object(activation.sources, "get_connector_source", AsyncMock(return_value=self.source)),
            patch.object(collection, "supports_native", lambda s: self.supported),
            patch.object(activation.provider_terms, "require_terms_eligible", AsyncMock(side_effect=self.terms)),
            patch.object(activation.scheduler, "upsert_schedule", AsyncMock()),
            patch("modules.settings.public.module_is_enabled", AsyncMock(return_value=True)),
        ]
        self.m = [p.start() for p in self.p]
        return self

    def __exit__(self, *exc):
        for p in self.p:
            p.stop()


async def activate(env):
    session = SimpleNamespace(get=AsyncMock(return_value=env.credential))
    return await activation.activate_native_in_uow(
        session, SOURCE_ID, env.fence, env.r, scope=object(), multi_workspace_enabled=True)


@pytest.mark.asyncio
async def test_native_activation_persists_applied_state_and_schedule_without_workflow():
    r = row(execution_backend="n8n", workflow_id=None, desired_enabled=False, state="saved_not_active", applied_revision=0,
            applied_backend_revision=0, backend_revision=2, transition_phase="activating_new", target_backend="native",
            applied_template_revision=0)
    with _NativeEnv(r, config={"schedule_interval_minutes": 360}) as env:
        assert await activate(env) == ""
    assert (r.execution_backend, r.state, r.desired_enabled, r.applied_revision) == ("native", "active", True, 3)
    assert r.applied_backend_revision == 2 and r.transition_phase == "idle" and r.workflow_id is None
    assert backends.backend_admits(r)
    kwargs = env.m[3].await_args.kwargs
    assert kwargs["interval_minutes"] == 360 and kwargs["enabled"] is True


@pytest.mark.asyncio
async def test_native_activation_refusals_change_nothing():
    cases = [
        (dict(local_only=True), "source_inactive"),
        (dict(supported=False), "native_unsupported"),
        (dict(terms=[HTTPException(status_code=409, detail="provider_terms_ineligible")]), "terms_not_accepted"),
    ]
    for kwargs, code in cases:
        r = row(execution_backend="n8n", state="saved_not_active", desired_enabled=False, applied_backend_revision=0)
        with _NativeEnv(r, **kwargs) as env:
            assert await activate(env) == code
        assert r.state == "saved_not_active" and r.execution_backend == "n8n"
        env.m[3].assert_not_awaited()


@pytest.mark.asyncio
async def test_native_header_auth_needs_a_ready_credential_for_this_revision():
    config = {"auth_method": "http_header", "auth_header_name": "X-Key"}
    good = SimpleNamespace(state="ready", encrypted_secret="x", source_generation=1, configuration_revision=3,
                           header_name="X-Key")
    for credential, expected in [
        (None, "invalid_credential"),
        (SimpleNamespace(**{**good.__dict__, "configuration_revision": 2}), "invalid_credential"),  # stale after a save
        (SimpleNamespace(**{**good.__dict__, "header_name": "Other"}), "invalid_credential"),
        (SimpleNamespace(**{**good.__dict__, "state": "revoked"}), "invalid_credential"),
        (good, ""),
    ]:
        r = row(desired_configuration=config, state="saved_not_active", desired_enabled=False)
        with _NativeEnv(r, credential=credential) as env:
            assert await activate(env) == expected


# --------------------------------------------------------------------------- REST credential + envelopes

def test_rest_secret_roundtrip_and_binding_fences():
    op = uuid4()
    kw = dict(source_id=SOURCE_ID, operation_id=op, source_generation=1, configuration_revision=3, header_name="X-Key")
    blob = encrypt_rest_secret(KEY, secret="s3cret-token", **kw)
    assert "s3cret" not in blob
    assert decrypt_rest_secret(KEY, blob, **kw) == "s3cret-token"
    for change in ({"configuration_revision": 4}, {"header_name": "Y"}, {"source_generation": 2},
                   {"operation_id": uuid4()}, {"source_id": uuid4()}):
        with pytest.raises(CredentialEncryptionUnavailable):
            decrypt_rest_secret(KEY, blob, **{**kw, **change})
    for bad in ("", "has space", "x" * 513):
        with pytest.raises(ValueError):
            encrypt_rest_secret(KEY, secret=bad, **kw)


@pytest.mark.parametrize("name,source_type", [("rest.json", "api"), ("rss.json", "rss"), ("url.json", "web")])
def test_packaged_templates_carry_the_backend_revision(name, source_type):
    source = SimpleNamespace(id=SOURCE_ID, type=source_type, provider=None, generation=4, configuration={})
    body = build_workflow(
        source, desired_revision=3, backend_revision=7, workflow_operation_id=uuid4(),
        collector_credential_id="c", manual_credential_id="m", provider_credential_id=None)
    text = json.dumps(body)
    assert "__BBD_BACKEND_REVISION__" not in text and "__BBD_CONNECTOR_REVISION__" not in text
    assert "backend_revision:7" in text.replace(" ", "")


def test_every_packaged_template_has_a_backend_placeholder():
    from pathlib import Path

    for path in Path("infrastructure/n8n/workflows").glob("*.json"):
        assert "__BBD_BACKEND_REVISION__" in path.read_text(encoding="utf-8"), path.name


# --------------------------------------------------------------------------- fences and routes

@pytest.mark.asyncio
@pytest.mark.parametrize("backend,row_changes,carried,expected", [
    ("n8n", {}, 1, True),
    ("n8n", {}, 5, False),  # late webhook from an older backend revision
    ("n8n", {"transition_phase": "deactivating_old"}, 1, False),
    ("native", {"execution_backend": "native", "workflow_id": None}, None, True),
    ("native", {"execution_backend": "native", "workflow_id": None}, 1, True),
    ("native", {"execution_backend": "native", "backend_revision": 2, "applied_backend_revision": 2}, 1, False),
])
async def test_collection_fence_proves_backend_and_carried_revision(backend, row_changes, carried, expected):
    r = row(**row_changes)
    source = SimpleNamespace(id=SOURCE_ID, workspace_id=uuid4(), status="active", generation=1)
    session = SimpleNamespace(scalar=AsyncMock(return_value=r))
    current = SimpleNamespace(workspace_id=source.workspace_id, status="active", generation=1)
    with patch.object(provisioning.sources, "get_source_fence", AsyncMock(return_value=current)):
        ok = await provisioning.require_collection_fence(
            session, source, 1, 3, backend_revision=carried, scope=object(), multi_workspace_enabled=True)
    assert ok is expected


@pytest.mark.asyncio
async def test_bearer_routes_reject_native_and_transitioning_rows():
    for r, rejected in [
        (row(), False), (row(execution_backend="native"), True), (row(transition_phase="activating_new"), True), (None, False),
    ]:
        session = SimpleNamespace(scalar=AsyncMock(return_value=r))
        if rejected:
            with pytest.raises(HTTPException) as caught:
                await provisioning.require_n8n_backend(session, SOURCE_ID)
            assert caught.value.detail == "backend_inactive"
        else:
            await provisioning.require_n8n_backend(session, SOURCE_ID)


def test_backend_choice_preserves_existing_and_defaults_fresh_supported_to_native():
    from modules.connectors import provisioning_routes as routes

    fresh = row(applied_revision=0, applied_backend_revision=0, workflow_id=None, state="saved_not_active")
    with patch.object(routes.collection, "supports_native", lambda s: True):
        assert routes._choose_backend(None, fresh, object()) == "native"
        assert routes._choose_backend(None, row(), object()) == "n8n"  # existing source keeps its backend
        assert routes._choose_backend(None, row(execution_backend="native"), object()) == "native"
        assert routes._choose_backend("n8n", fresh, object()) == "n8n"
        pending = row(transition_phase="activating_new", target_backend="native")
        assert routes._choose_backend(None, pending, object()) == "native"
        with pytest.raises(HTTPException):
            routes._choose_backend("n8n", pending, object())
    with patch.object(routes.collection, "supports_native", lambda s: False):
        assert routes._choose_backend(None, fresh, object()) == "n8n"


@pytest.mark.asyncio
async def test_reentry_stores_encrypted_secret_bumps_revision_and_lifts_the_gate():
    from modules.connectors import provisioning_routes as routes

    r = row(desired_configuration={"auth_method": "http_header", "auth_header_name": "X-Key"}, credential_revision=4)
    source = SimpleNamespace(id=SOURCE_ID, status="active", type="api", provider=None, generation=1)
    stored = []
    session = SimpleNamespace(
        rollback=AsyncMock(), add=stored.append, get=AsyncMock(return_value=None))
    settings = SimpleNamespace(
        multi_workspace_enabled=True,
        connector_credential_encryption_key=SimpleNamespace(get_secret_value=lambda: KEY))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=settings)))
    payload = routes.NativeRestCredentialRequest(expected_revision=3, secret="top-secret-value")
    clear = AsyncMock(return_value=True)
    fence = SimpleNamespace(status="active", generation=1)
    with (
        patch.object(routes, "_owner_access", AsyncMock(return_value=object())),
        patch.object(routes, "_source", AsyncMock(return_value=source)),
        patch.object(routes.provisioning, "activation_status", AsyncMock(return_value=r)),
        patch.object(routes.provisioning, "lock_connector", AsyncMock(return_value=(fence, r, {}))),
        patch.object(routes.scheduler, "clear_collection_block", clear),
        patch.object(routes, "commit_with_replay", AsyncMock()),
        patch.object(routes, "make_source_change", lambda *a, **k: None),
        patch.object(routes, "_activation_read", AsyncMock(return_value="read")),
    ):
        result = await routes.reenter_native_rest_credential(
            SOURCE_ID, payload, session, request, SimpleNamespace(workspace_id=uuid4()))
    assert result == "read" and r.credential_revision == 5
    clear.assert_awaited_once()
    assert clear.await_args.kwargs == {"credential_revision": 5}
    (credential,) = stored
    assert credential.state == "ready" and credential.configuration_revision == 3
    assert "top-secret" not in credential.encrypted_secret
    assert decrypt_rest_secret(
        KEY, credential.encrypted_secret, source_id=SOURCE_ID, operation_id=credential.operation_id,
        source_generation=1, configuration_revision=3, header_name="X-Key") == "top-secret-value"


@pytest.mark.asyncio
async def test_reentry_refuses_sources_without_header_authentication():
    from modules.connectors import provisioning_routes as routes

    r = row(desired_configuration={"auth_method": "none"})
    source = SimpleNamespace(id=SOURCE_ID, status="active", type="api", provider=None, generation=1)
    settings = SimpleNamespace(
        multi_workspace_enabled=True, connector_credential_encryption_key=SimpleNamespace(get_secret_value=lambda: KEY))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=settings)))
    with (
        patch.object(routes, "_owner_access", AsyncMock(return_value=object())),
        patch.object(routes, "_source", AsyncMock(return_value=source)),
        patch.object(routes.provisioning, "activation_status", AsyncMock(return_value=r)),
        pytest.raises(HTTPException) as caught,
    ):
        await routes.reenter_native_rest_credential(
            SOURCE_ID, routes.NativeRestCredentialRequest(expected_revision=3, secret="abc"), SimpleNamespace(),
            request, SimpleNamespace(workspace_id=uuid4()))
    assert caught.value.status_code == 422


@pytest.mark.asyncio
async def test_credential_gate_lifts_only_at_a_newer_credential_revision():
    from modules.connectors import scheduler

    def gate():
        return SimpleNamespace(
            blocked_error_code="invalid_credential", blocked_dimensions=["credential"],
            blocked_connector_revision=3, blocked_credential_revision=4, blocked_terms_revision=None)

    session = SimpleNamespace(flush=AsyncMock())
    for revision, lifted in [(4, False), (None, False), (5, True)]:
        schedule = gate()
        session.get = AsyncMock(return_value=schedule)
        assert await scheduler.clear_collection_block(session, SOURCE_ID, credential_revision=revision) is lifted
        assert (schedule.blocked_error_code is None) is lifted
