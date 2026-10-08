"""W2-S caller conversions: mapping, MCP collect, connector worker and GitHub routes (mocked, no DB).

Callees owned by the Documents and Knowledge slices are mocked with their frozen new signatures.
"""

import ast
import inspect
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from core.workspaces.schemas import AccessFence, InternalJobScope, WorkspaceContext
from modules.connectors import mcp, worker
from modules.connectors import public as connectors_public
from modules.connectors.github import mapping, routes

WORKSPACE_ID = uuid4()
SOURCE_ID = uuid4()
OPERATION_ID = uuid4()
SCOPE = InternalJobScope(
    workspace_id=WORKSPACE_ID, actor_user_id=7, membership_revision=3, source_id=SOURCE_ID, source_generation=4,
)
FENCE = AccessFence(WORKSPACE_ID, 7, 3, 5)
OWNER = WorkspaceContext(user_id=7, workspace_id=WORKSPACE_ID, role="owner", membership_revision=3)
MEMBER = WorkspaceContext(user_id=8, workspace_id=WORKSPACE_ID, role="member", membership_revision=3)


def _envelope(**changes) -> dict:
    envelope = {
        "id": str(OPERATION_ID), "kind": "delete", "state": "prepared", "target_id": "cred-1",
        "workspace_id": str(WORKSPACE_ID), "actor_user_id": 7, "membership_revision": 3,
        "workspace_configuration_revision": 5, "source_generation": 4, "revision": 2,
    }
    envelope.update(changes)
    return envelope


# ---------------------------------------------------------------- mapping

def _mapping_session() -> MagicMock:
    session = MagicMock()
    nested = MagicMock()
    nested.__aenter__ = AsyncMock(return_value=None)
    nested.__aexit__ = AsyncMock(return_value=False)
    session.begin_nested.return_value = nested
    return session


async def test_map_github_version_threads_scope_to_every_callee() -> None:
    ready = SimpleNamespace(
        source_id=SOURCE_ID, source_generation=4, document_id=uuid4(), document_version_id=uuid4(),
    )
    snapshot = SimpleNamespace(
        provider_metadata=SimpleNamespace(source_fields={"record_type": "issue"}),
        canonical_url="https://github.com/a/b/issues/1", title="T", excerpt="E", provider_id="p1",
        observed_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    chunk_id, entity_id, membership_id = uuid4(), uuid4(), uuid4()
    source = SimpleNamespace(provider="github", generation=4, configuration={})
    patches = {
        "sources.get_connector_source": AsyncMock(return_value=source),
        "documents.read_provider_snapshots": AsyncMock(return_value=[snapshot]),
        "documents.get_first_chunk_id": AsyncMock(return_value=chunk_id),
        "documents.read_extraction_evidence_refs": AsyncMock(return_value=[object()]),
        "entities.find_extraction_entity": AsyncMock(return_value=None),
        "entities.create_extracted_entity": AsyncMock(return_value=entity_id),
        "entities.record_extraction_membership": AsyncMock(return_value=membership_id),
        "entities.publish_derived_field": AsyncMock(return_value=True),
        "relationships.publish_extracted_relationship": AsyncMock(return_value=uuid4()),
        "timeline.publish_provider_event": AsyncMock(return_value=uuid4()),
    }
    from contextlib import ExitStack

    with ExitStack() as stack:
        mocks = {name: stack.enter_context(patch.object(getattr(mapping, name.split(".")[0]), name.split(".")[1], mock))
                 for name, mock in patches.items()}
        assert await mapping.map_github_version(
            _mapping_session(), ready, scope=SCOPE, multi_workspace_enabled=False,
        ) is True
    for name, mock in mocks.items():
        assert mock.await_count >= 1, name
        for call in mock.await_args_list:
            assert call.kwargs["scope"] is SCOPE, name
            assert call.kwargs["multi_workspace_enabled"] is False, name


async def test_map_github_version_foreign_source_maps_nothing() -> None:
    ready = SimpleNamespace(source_id=SOURCE_ID, source_generation=4)
    with patch.object(mapping.sources, "get_connector_source", AsyncMock(return_value=None)), \
            patch.object(mapping.documents, "read_provider_snapshots", AsyncMock()) as snapshots:
        assert await mapping.map_github_version(
            _mapping_session(), ready, scope=SCOPE, multi_workspace_enabled=False,
        ) is False
    snapshots.assert_not_awaited()


async def test_public_map_github_version_does_not_read_principal_off_ready_ref() -> None:
    """ReadyVersionRef has no workspace/actor/membership; the admitted scope is authoritative."""
    ready = SimpleNamespace(source_id=SOURCE_ID, source_generation=4)  # no principal attributes at all
    source = SimpleNamespace(provider="github", status="active", generation=4)
    inner = AsyncMock(return_value=True)
    with patch.object(connectors_public, "_connector_access", AsyncMock(return_value=FENCE)), \
            patch.object(connectors_public, "_read_scoped_source", AsyncMock(return_value=source)) as read, \
            patch("modules.connectors.github.mapping.map_github_version", inner):
        assert await connectors_public.map_github_version(
            MagicMock(), ready, scope=SCOPE, multi_workspace_enabled=False,
        ) is True
    assert read.await_args.kwargs["scope"] is SCOPE
    assert inner.await_args.kwargs == {"scope": SCOPE, "multi_workspace_enabled": False}


@pytest.mark.parametrize("source", [None, SimpleNamespace(provider="github", status="active", generation=9),
                                    SimpleNamespace(provider="telegram", status="active", generation=4)])
async def test_public_map_github_version_rejects_foreign_or_stale_source(source) -> None:
    ready = SimpleNamespace(source_id=SOURCE_ID, source_generation=4)
    inner = AsyncMock(return_value=True)
    with patch.object(connectors_public, "_connector_access", AsyncMock(return_value=FENCE)), \
            patch.object(connectors_public, "_read_scoped_source", AsyncMock(return_value=source)), \
            patch("modules.connectors.github.mapping.map_github_version", inner):
        assert await connectors_public.map_github_version(
            MagicMock(), ready, scope=SCOPE, multi_workspace_enabled=False,
        ) is False
    inner.assert_not_awaited()


# ---------------------------------------------------------------- MCP collect

def test_collect_signature_matches_frozen_contract() -> None:
    parameters = list(inspect.signature(mcp.collect).parameters.values())
    assert [p.name for p in parameters] == [
        "runtime", "session_factory", "source_id", "expected_connector_revision", "expected_generation",
        "collector_token", "expected_connection_id", "scope", "multi_workspace_enabled",
    ]
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in parameters[-2:])
    assert "owner_id" not in inspect.signature(mcp.collect).parameters


async def test_collect_rejects_scope_bound_to_another_source_before_any_session() -> None:
    factory = MagicMock()
    with pytest.raises(mcp.McpCollectionError) as stale:
        await mcp.collect(
            MagicMock(), factory, uuid4(), 1, 4, scope=SCOPE, multi_workspace_enabled=False,
        )
    assert str(stale.value) == "mcp_source_stale" or getattr(stale.value, "code", None) == "mcp_source_stale"
    factory.assert_not_called()


async def test_collection_fence_orders_access_source_provisioning() -> None:
    calls: list[str] = []
    source = SimpleNamespace(provider="mcp")

    async def access(*args, **kwargs):
        calls.append("access")
        return FENCE

    async def lock_source(*args, **kwargs):
        calls.append("source")
        assert kwargs["expected_access_fence"] == FENCE
        return SimpleNamespace(id=SOURCE_ID)

    async def provisioning_fence(*args, **kwargs):
        calls.append("provisioning")
        assert kwargs["lock"] is True and kwargs["scope"] is SCOPE
        return True

    with patch.object(mcp, "lock_access_fence", access), \
            patch.object(mcp.sources, "lock_source", lock_source), \
            patch.object(mcp.sources, "get_connector_source", AsyncMock(return_value=source)), \
            patch("modules.settings.public.module_is_enabled", AsyncMock(return_value=True)), \
            patch("modules.connectors.provisioning.require_collection_fence", provisioning_fence):
        assert await mcp._collection_fence(
            MagicMock(), SOURCE_ID, 4, 2, scope=SCOPE, multi_workspace_enabled=False,
        ) == FENCE
    assert calls == ["access", "source", "provisioning"]


@pytest.mark.parametrize("status", [401, 403, 404, 409])
async def test_collection_fence_denied_admission_is_not_current(status) -> None:
    with patch.object(mcp, "lock_access_fence", AsyncMock(side_effect=HTTPException(status_code=status))):
        assert await mcp._collection_fence(
            MagicMock(), SOURCE_ID, 4, 2, scope=SCOPE, multi_workspace_enabled=False,
        ) is None


async def test_record_result_uses_original_fence_and_replay_gate() -> None:
    session = MagicMock()
    session.rollback = AsyncMock()
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    source = SimpleNamespace(id=SOURCE_ID, generation=4)
    current = SimpleNamespace(id=SOURCE_ID, generation=4, status="active")
    record, replay = AsyncMock(return_value=True), AsyncMock()
    with patch.object(mcp, "lock_access_fence", AsyncMock(return_value=FENCE)), \
            patch.object(mcp.sources, "record_collection_result", record), \
            patch.object(mcp.sources, "lock_source", AsyncMock(return_value=current)), \
            patch.object(mcp, "commit_with_replay", replay):
        await mcp._record_result(factory, source, None, no_changes=True, scope=SCOPE, multi_workspace_enabled=False)
    assert record.await_args.kwargs["expected_access_fence"] == FENCE
    assert replay.await_args.kwargs["access_fence"] == FENCE
    assert replay.await_args.kwargs["scope"] is SCOPE
    assert replay.await_args.args[1][0].principal_user_id == 7


# ---------------------------------------------------------------- worker lineage

def test_lineage_is_rebuilt_from_the_recorded_envelope() -> None:
    scope, fence = worker._lineage(SOURCE_ID, _envelope())
    assert scope == SCOPE
    assert fence == FENCE


@pytest.mark.parametrize("envelope", [None, {}, _envelope(actor_user_id="7"), _envelope(source_generation=0)])
def test_legacy_or_malformed_envelope_is_quarantined(envelope) -> None:
    for missing in ("workspace_configuration_revision", "workspace_id"):
        assert worker._lineage(SOURCE_ID, {k: v for k, v in _envelope().items() if k != missing}) is None
    assert worker._lineage(SOURCE_ID, envelope) is None


async def test_admission_requires_the_recorded_fence_exactly() -> None:
    session = MagicMock()
    session.rollback = AsyncMock()
    with patch.object(worker, "read_access_fence", AsyncMock(return_value=AccessFence(WORKSPACE_ID, 7, 3, 6))):
        assert await worker._admit_lineage(session, SOURCE_ID, _envelope(), multi_workspace_enabled=False) is None
    session.rollback.assert_awaited()
    with patch.object(worker, "read_access_fence", AsyncMock(return_value=FENCE)):
        assert await worker._admit_lineage(session, SOURCE_ID, _envelope(), multi_workspace_enabled=False) == (SCOPE, FENCE)


@pytest.mark.parametrize("status", [401, 403, 404, 409])
async def test_denied_admission_skips_and_other_errors_propagate(status) -> None:
    session = MagicMock()
    session.rollback = AsyncMock()
    with patch.object(worker, "read_access_fence", AsyncMock(side_effect=HTTPException(status_code=status))):
        assert await worker._admit_lineage(session, SOURCE_ID, _envelope(), multi_workspace_enabled=False) is None
    with patch.object(worker, "read_access_fence", AsyncMock(side_effect=HTTPException(status_code=503))), \
            pytest.raises(HTTPException):
        await worker._admit_lineage(session, SOURCE_ID, _envelope(), multi_workspace_enabled=False)


def test_discovery_pages_by_source_id_after_the_cursor_before_limit() -> None:
    from sqlalchemy import select

    from modules.connectors.models import ConnectorProvisioning

    after = uuid4()
    statement = worker._page(
        select(ConnectorProvisioning.source_id), ConnectorProvisioning.source_id, after,
        ConnectorProvisioning.desired_enabled.is_(True),
    )
    sql = str(statement.compile(dialect=postgresql.dialect()))
    assert sql.index("source_id >") < sql.index("ORDER BY") < sql.index("LIMIT")


async def test_cursor_wraps_when_state_is_absent_or_page_is_short() -> None:
    ctx: dict[str, object] = {"w2_cursor_state": {}}
    last = uuid4()
    await worker._write_cursor(ctx, "connectors_workflows", last)
    assert await worker._read_cursor(ctx, "connectors_workflows") == last
    await worker._write_cursor(ctx, "connectors_workflows", None)
    assert await worker._read_cursor(ctx, "connectors_workflows") is None
    assert await worker._read_cursor(None, "connectors_workflows") is None
    await worker._write_cursor(None, "connectors_workflows", last)  # no shared state: starts over, never raises
    with pytest.raises(KeyError):
        await worker._read_cursor(ctx, "not_allowed")
    assert worker._cursor_ctx({}) is None and worker._cursor_ctx(ctx) is ctx


# ---------------------------------------------------------------- worker credential delete

def _delete_harness(envelope: dict, *, delete=None, retained_denied: bool = False):
    session = MagicMock()
    session.scalar = AsyncMock(return_value=envelope)
    session.rollback = AsyncMock()
    session.commit = AsyncMock()
    claimed = {**envelope, "state": "dispatched", "dispatch_started_at": "2026-01-01T00:00:00+00:00"}
    prov = SimpleNamespace(
        RetainedEffectAdmissionDenied=worker.provisioning.RetainedEffectAdmissionDenied,
        claim_credential_operation=AsyncMock(return_value=claimed),
        lock_retained_connector_effect=AsyncMock(
            side_effect=worker.provisioning.RetainedEffectAdmissionDenied(status_code=404, detail="gone")
            if retained_denied else None, return_value="BEFORE"),
        acknowledge_credential_delete=AsyncMock(return_value=True),
        fail_credential_operation=AsyncMock(return_value=True),
        commit_retained_connector_effect=AsyncMock(),
        record_retained_credential_result_in_uow=AsyncMock(return_value="stored"),
    )
    client = SimpleNamespace(delete=delete or AsyncMock())
    return session, prov, client, claimed


async def test_delete_success_acknowledges_under_original_scope_and_fence() -> None:
    session, prov, client, claimed = _delete_harness(_envelope())
    with patch.object(worker, "read_access_fence", AsyncMock(return_value=FENCE)), \
            patch.object(worker, "provisioning", prov):
        assert await worker._delete_credential(
            session, client, SOURCE_ID, "collector", OPERATION_ID, multi_workspace_enabled=False,
        ) is True
    client.delete.assert_awaited_once_with("cred-1")
    for mock in (prov.claim_credential_operation, prov.lock_retained_connector_effect,
                 prov.acknowledge_credential_delete, prov.commit_retained_connector_effect):
        assert mock.await_args.kwargs["scope"] == SCOPE
    for mock in (prov.lock_retained_connector_effect, prov.acknowledge_credential_delete,
                 prov.commit_retained_connector_effect):
        assert mock.await_args.kwargs["access_fence"] == FENCE
        assert mock.await_args.kwargs["original_operation"] is claimed
    prov.fail_credential_operation.assert_not_awaited()


async def test_delete_rejection_is_known_and_timeout_is_unknown() -> None:
    for status, unknown, code in ((400, False, "credential_delete_rejected"),
                                  (503, True, "credential_delete_outcome_unknown")):
        response = httpx.Response(status, request=httpx.Request("DELETE", "http://n8n"))
        failing = AsyncMock(side_effect=httpx.HTTPStatusError("x", request=response.request, response=response))
        session, prov, client, _claimed = _delete_harness(_envelope(), delete=failing)
        with patch.object(worker, "read_access_fence", AsyncMock(return_value=FENCE)), \
                patch.object(worker, "provisioning", prov):
            assert await worker._delete_credential(
                session, client, SOURCE_ID, "collector", OPERATION_ID, multi_workspace_enabled=False,
            ) is False
        args, kwargs = prov.fail_credential_operation.await_args
        assert args[4] == code and kwargs["unknown"] is unknown
        prov.acknowledge_credential_delete.assert_not_awaited()


async def test_delete_after_access_loss_journals_the_transport_and_changes_nothing_else() -> None:
    session, prov, client, _claimed = _delete_harness(_envelope(), retained_denied=True)
    with patch.object(worker, "read_access_fence", AsyncMock(return_value=FENCE)), \
            patch.object(worker, "provisioning", prov):
        assert await worker._delete_credential(
            session, client, SOURCE_ID, "collector", OPERATION_ID, multi_workspace_enabled=False,
        ) is False
    journal = prov.record_retained_credential_result_in_uow
    assert journal.await_args.kwargs["outcome"] == "known_success"
    assert journal.await_args.kwargs["access_fence"] == FENCE
    session.commit.assert_awaited_once()
    prov.acknowledge_credential_delete.assert_not_awaited()
    prov.commit_retained_connector_effect.assert_not_awaited()


@pytest.mark.parametrize("fence", [AccessFence(WORKSPACE_ID, 7, 3, 6), None])
async def test_delete_never_claims_or_sends_without_the_original_admission(fence) -> None:
    session, prov, client, _claimed = _delete_harness(_envelope())
    read = AsyncMock(return_value=fence) if fence else AsyncMock(side_effect=HTTPException(status_code=404))
    with patch.object(worker, "read_access_fence", read), patch.object(worker, "provisioning", prov):
        assert await worker._delete_credential(
            session, client, SOURCE_ID, "collector", OPERATION_ID, multi_workspace_enabled=False,
        ) is False
    prov.claim_credential_operation.assert_not_awaited()
    client.delete.assert_not_awaited()


async def test_revoke_deleted_grants_skips_a_source_without_admissible_scope() -> None:
    settings = SimpleNamespace(
        multi_workspace_enabled=False,
        connector_credential_encryption_key=SimpleNamespace(get_secret_value=lambda: "k"),
    )
    session = MagicMock()
    session.execute = AsyncMock(return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [SOURCE_ID])))
    session.rollback = AsyncMock()
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    lock = AsyncMock()
    with patch.object(worker.sources, "resolve_source_job_scope", AsyncMock(return_value=None)), \
            patch.object(worker.provisioning, "lock_connector", lock):
        assert await worker.revoke_deleted_source_github_grants(factory, settings, cursor_ctx={"w2_cursor_state": {}}) == 0
    lock.assert_not_awaited()


# ---------------------------------------------------------------- routes

def test_every_oauth_operation_insert_carries_the_real_workspace_and_actor() -> None:
    tree = ast.parse(inspect.getsource(routes))
    inserts = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
               and getattr(n.func, "id", None) == "GithubOAuthOperation"]
    assert len(inserts) == 3
    for call in inserts:
        keywords = {k.arg: ast.unparse(k.value) for k in call.keywords}
        assert keywords["workspace_id"] == "scope.workspace_id"
        assert keywords["owner_id"] == "actor"


def test_no_route_keys_oauth_state_on_the_session_owner_id() -> None:
    source = inspect.getsource(routes)
    # The browser-session check may still compare the session's owner; OAuth state may not be keyed on it.
    assert "GithubOAuthCoordinator.owner_id == owner" not in source
    assert "GithubOAuthCoordinator.owner_id == _owner" not in source
    assert "GithubOAuthOperation.owner_id == owner" not in source
    assert "owner_id=owner" not in source and "owner_id=_owner" not in source


@pytest.mark.parametrize("name", [
    "start_github_authorization", "github_connection_status", "reset_github_sync",
    "acknowledge_github_reconnect", "refresh_github_authorization", "complete_github_authorization",
    "github_project_summary", "list_github_grant_peers", "revoke_github_grant",
])
def test_routes_keep_their_owner_dependency_and_add_the_workspace(name) -> None:
    parameters = inspect.signature(getattr(routes, name)).parameters
    assert "scope" in parameters
    assert str(parameters["scope"].annotation).count("require_workspace") == 1
    assert any(str(p.annotation).count("require_owner") for p in parameters.values())


def test_peer_inventory_filters_the_workspace_before_limit() -> None:
    sql = str(routes._peer_inventory("123", OWNER).compile(dialect=postgresql.dialect()))
    assert sql.index("workspace_id") < sql.index("ORDER BY") < sql.index("LIMIT")


async def test_member_is_denied_before_any_lock() -> None:
    with patch.object(routes, "lock_access_fence", AsyncMock()) as lock, \
            pytest.raises(HTTPException) as denied:
        await routes._admit(MagicMock(), MagicMock(), MEMBER)
    assert denied.value.status_code == 403
    lock.assert_not_awaited()


async def test_reconciliation_mark_writes_nothing_when_original_access_is_gone() -> None:
    session = MagicMock()
    session.rollback = AsyncMock()
    session.scalar = AsyncMock()
    request = MagicMock()
    request.app.state.settings.multi_workspace_enabled = False
    with patch.object(routes, "_admit", AsyncMock(side_effect=HTTPException(status_code=409))):
        await routes._mark_github_reconciliation(
            session, request, OWNER, FENCE, SOURCE_ID, OPERATION_ID, "authorization_outcome_unknown",
        )
    session.scalar.assert_not_awaited()
    session.rollback.assert_awaited_once()


async def test_receiver_checks_build_availability_not_a_global_database_flag() -> None:
    source = inspect.getsource(routes.receive_github_webhook)
    assert "module_is_enabled" not in source and "register_modules" in source


# ---------------------------------------------------------------- fix round 1


def _session() -> MagicMock:
    session = MagicMock()
    for name in ("rollback", "commit", "execute", "scalar"):
        setattr(session, name, AsyncMock())
    return session


@pytest.mark.parametrize("foreign", [False, True])
async def test_denied_phase_three_releases_only_our_own_coordinator_claim(foreign) -> None:

    settings = SimpleNamespace(
        multi_workspace_enabled=False,
        connector_credential_encryption_key=SimpleNamespace(get_secret_value=lambda: "k"),
    )
    grant = SimpleNamespace(
        encrypted_tokens=b"x", error_code=worker._DELETED_SOURCE_REVOKE, source_id=SOURCE_ID,
        operation_id=uuid4(), source_generation=4, configuration_revision=1, token_revision=1,
    )
    coordinator = SimpleNamespace(
        state="idle", operation_id=None, error_code=None, updated_at=datetime.now(UTC),
    )
    listing, phase1, phase3 = _session(), _session(), _session()
    listing.execute.return_value = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [SOURCE_ID]))
    phase1.scalar.side_effect = [grant, coordinator]
    phase3.scalar.side_effect = [coordinator]
    sessions = iter([listing, phase1, phase3])
    factory = MagicMock()
    factory.side_effect = lambda: MagicMock(
        __aenter__=AsyncMock(return_value=next(sessions)), __aexit__=AsyncMock(return_value=False),
    )
    other = uuid4()

    async def provider_revoke(*_args):
        if foreign:
            coordinator.operation_id = other

    with patch.object(worker, "_admit_source_job", AsyncMock(return_value=(SCOPE, FENCE))), \
            patch.object(worker.provisioning, "lock_connector", AsyncMock(
                side_effect=[(object(), None, ()), HTTPException(status_code=403)])), \
            patch.object(worker.provisioning, "github_grant_has_active_peer", AsyncMock(return_value=False)), \
            patch("modules.connectors.github.oauth._open_token_cipher", return_value={"access_token": "t"}), \
            patch("modules.connectors.github.oauth.revoke_github_grant", AsyncMock(side_effect=provider_revoke)), \
            patch.object(worker, "commit_with_replay", AsyncMock()), \
            patch.object(worker, "select", MagicMock()):
        assert await worker.revoke_deleted_source_github_grants(factory, settings, cursor_ctx={"w2_cursor_state": {}}) == 0
    if foreign:
        assert coordinator.operation_id == other and coordinator.state == "revoking"
        phase3.commit.assert_not_awaited()
    else:
        assert (coordinator.state, coordinator.operation_id, coordinator.error_code) == ("idle", None, None)
        phase3.commit.assert_awaited_once()


def test_credential_discovery_cursor_is_a_composite_keyset() -> None:
    from sqlalchemy import select

    from modules.connectors.models import ConnectorManagedCredential as Credential

    after = uuid4()
    sql = str(worker._page(
        select(Credential.source_id, Credential.slot), Credential.source_id, after,
        order=(Credential.slot,), slot_column=Credential.slot, after_slot="b",
    ).compile(dialect=postgresql.dialect()))
    assert "source_id >" in sql and "source_id =" in sql and "slot >" in sql
    assert sql.index("slot >") < sql.index("ORDER BY") < sql.index("LIMIT")
    ctx: dict[str, object] = {"w2_cursor_state": {}}
    worker._write_slot_cursor(ctx, "k", (after, "b"))
    assert worker._read_slot_cursor(ctx, "k") == (after, "b")
    worker._write_slot_cursor(ctx, "k", None)
    assert worker._read_slot_cursor(ctx, "k") is None
    assert worker._read_slot_cursor({"w2_cursor_state": {"k": "garbage"}}, "k") is None


async def test_changed_access_fence_between_start_and_record_is_denied_and_publishes_nothing() -> None:
    session = MagicMock()
    session.rollback = AsyncMock()
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    source = SimpleNamespace(id=SOURCE_ID, generation=4)
    record, replay = AsyncMock(), AsyncMock()
    stale = AccessFence(WORKSPACE_ID, 7, 3, 4)
    access = AsyncMock(side_effect=HTTPException(status_code=409))
    with patch.object(mcp, "lock_access_fence", access), \
            patch.object(mcp.sources, "record_collection_result", record), \
            patch.object(mcp, "commit_with_replay", replay):
        await mcp._record_result(
            factory, source, None, scope=SCOPE, multi_workspace_enabled=False, expected_access_fence=stale,
        )
    assert access.await_args.kwargs["expected"] == stale
    record.assert_not_awaited()
    replay.assert_not_awaited()
