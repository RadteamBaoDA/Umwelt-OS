"""Exercise deletion trust boundaries and durable failure recovery without external services.

The ASGI tests retain real auth/CSRF/rate-limit/lock helpers. Persistence adapters are
in-memory query doubles; lock ordering and SQL scope assertions are not PostgreSQL proof.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr
from redis.exceptions import RedisError
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError

from core.auth import deletion_cleanup, deletion_routes, deletion_worker, routes
from core.auth.deletion_models import AccountDeletionOperation
from core.auth.dependencies import CSRF_COOKIE, SESSION_COOKIE, _hash
from core.auth.erasure_contracts import AccountErasureScope, ErasureProgress
from core.auth.models import AuthSession, Owner
from core.auth.public import get_active_account
from core.auth.service import hash_password
from core.database import get_session
from core.workspaces.models import Workspace, WorkspaceInvitation, WorkspaceMembership

PASSWORD = "Original-password-123!"
DELETION_PATH = "/api/v1/auth/account/deletion"


class CounterRedis:
    """Execute the INCR/EXPIRE pipeline shape used by the real auth rate limiter."""

    def __init__(self):
        self.counts = {}
        self.commands = []

    def pipeline(self, transaction=True):
        assert transaction
        self.commands = []
        return self

    def incr(self, key):
        self.commands.append(("incr", key))

    def expire(self, key, seconds, **kwargs):
        assert seconds == 120 and kwargs == {"nx": True}
        self.commands.append(("expire", key))

    async def execute(self):
        results = []
        for command, key in self.commands:
            if command == "incr":
                self.counts[key] = self.counts.get(key, 0) + 1
                results.append(self.counts[key])
            else:
                results.append(True)
        return results


@pytest.fixture
def admission(monkeypatch):
    """Keep auth dependencies, password verification and lifecycle queries real at ASGI boundary."""
    now = datetime.now(UTC)
    workspace_id = uuid4()
    settings = SimpleNamespace(
        public_origin="http://testserver", csrf_signing_secret=SecretStr("unit-only-signing-secret"),
        setup_token=SecretStr("unit-only-setup-token"), secure_cookies=False,
        multi_workspace_enabled=True, session_idle_hours=8,
        auth_client_ip_header="", auth_trust_forwarded_for=False,
    )
    csrf_token, csrf_cookie = routes._new_csrf(settings)
    owner = Owner(
        id=7, password_hash=hash_password(PASSWORD), account_state="active",
        email="owner@example.test", default_workspace_id=workspace_id,
        created_at=now, email_verified_at=None, email_verification_source=None,
    )
    auth = AuthSession(
        token_hash=_hash("session-token"), owner_id=7, csrf_hash=_hash(csrf_token),
        created_at=now, last_seen_at=now, expires_at=now + timedelta(hours=1),
    )
    state = SimpleNamespace(owner=owner, auth=auth, member=None, invitation=None, operation=None, queries=[])
    session = MagicMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    session.flush = AsyncMock()
    session.execute = AsyncMock()
    session.get = AsyncMock(side_effect=lambda model, key: state.auth if model is AuthSession and key == auth.token_hash else None)

    async def scalar(query):
        state.queries.append(query)
        entity = query.column_descriptions[0]["entity"]
        if entity is Owner:
            return state.owner
        if entity is AuthSession:
            return state.auth
        if entity is Workspace:
            return SimpleNamespace(id=workspace_id, owner_user_id=7)
        if entity is WorkspaceMembership:
            return state.member
        if entity is WorkspaceInvitation:
            return state.invitation
        if entity is AccountDeletionOperation:
            digest = query.compile().params.get("receipt_hash_1")
            return state.operation if state.operation is not None and state.operation.receipt_hash == digest else None
        raise AssertionError(f"Unexpected persistence query: {query}")

    session.scalar = AsyncMock(side_effect=scalar)
    context = AsyncMock(return_value=SimpleNamespace(
        role="owner", user_id=7, workspace_id=workspace_id,
    ))
    monkeypatch.setattr("core.auth.public.resolve_workspace_context", context)
    app = FastAPI()
    app.state.settings = settings
    app.state.redis = CounterRedis()
    app.include_router(deletion_routes.router)
    app.include_router(routes.router)

    async def provide_session():
        yield session

    app.dependency_overrides[get_session] = provide_session
    return SimpleNamespace(
        app=app, state=state, session=session, csrf=csrf_token, workspace_id=workspace_id,
        cookies={SESSION_COOKIE: "session-token", CSRF_COOKIE: csrf_cookie,
                 deletion_routes.RECEIPT_COOKIE: "r" * 43},
        headers={"Origin": "http://testserver", "X-CSRF-Token": csrf_token},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("fault,status", [
    ("missing-session", 401), ("unknown-session", 401), ("disabled-account", 401),
    ("missing-origin", 403), ("foreign-origin", 403), ("missing-csrf", 403),
    ("tampered-cookie", 403), ("other-session-csrf", 403),
])
async def test_deletion_asgi_rejects_invalid_auth_origin_and_session_bound_csrf(admission, fault, status):
    """Reject invalid browser authority before consuming rate limits or mutating identity."""
    cookies, headers = dict(admission.cookies), dict(admission.headers)
    if fault == "missing-session":
        cookies.pop(SESSION_COOKIE)
    elif fault == "unknown-session":
        cookies[SESSION_COOKIE] = "another-session"
    elif fault == "disabled-account":
        admission.state.owner.account_state = "disabled"
    elif fault == "missing-origin":
        headers.pop("Origin")
    elif fault == "foreign-origin":
        headers["Origin"] = "https://attacker.example"
    elif fault == "missing-csrf":
        headers.pop("X-CSRF-Token")
    elif fault == "tampered-cookie":
        cookies[CSRF_COOKIE] += "bad"
    else:
        token, cookies[CSRF_COOKIE] = routes._new_csrf(admission.app.state.settings)
        headers["X-CSRF-Token"] = token
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=admission.app), base_url="http://testserver", cookies=cookies) as client:
        response = await client.post(DELETION_PATH, json={"current_password": PASSWORD, "confirmation": "DELETE"}, headers=headers)
    assert response.status_code == status
    admission.session.add.assert_not_called()
    admission.session.commit.assert_not_awaited()
    assert not admission.app.state.redis.counts


@pytest.mark.asyncio
async def test_wrong_password_attempts_are_throttled_before_sixth_password_check(admission, monkeypatch):
    """Use the real password verifier and limiter to bound repeated destructive attempts."""
    from core.auth.service import verify_password

    verifier = MagicMock(wraps=verify_password)
    monkeypatch.setattr(deletion_routes, "verify_password", verifier)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=admission.app), base_url="http://testserver", cookies=admission.cookies) as client:
        responses = [await client.post(DELETION_PATH, headers=admission.headers,
                                      json={"current_password": "wrong", "confirmation": "DELETE"}) for _ in range(6)]
    assert [response.status_code for response in responses] == [400] * 5 + [429]
    assert int(responses[-1].headers["Retry-After"]) > 0
    assert verifier.call_count == 5
    admission.session.add.assert_not_called()
    admission.session.commit.assert_not_awaited()
    assert admission.state.owner.account_state == "active"


@pytest.mark.asyncio
async def test_deletion_rate_limit_fails_closed_on_redis_failure(admission):
    """A missing limiter cannot allow acceptance or expose its internal failure details."""
    admission.app.state.redis.execute = AsyncMock(side_effect=RedisError("internal detail"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=admission.app), base_url="http://testserver", cookies=admission.cookies) as client:
        response = await client.post(DELETION_PATH, headers=admission.headers,
                                     json={"current_password": PASSWORD, "confirmation": "DELETE"})
    assert response.status_code == 503
    assert "internal detail" not in response.text
    admission.session.add.assert_not_called()
    admission.session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_receipt_is_required_before_submission_and_wrong_confirmation_never_writes(admission):
    """Require previously established recovery authority and literal destructive confirmation."""
    cookies = {key: value for key, value in admission.cookies.items() if key != deletion_routes.RECEIPT_COOKIE}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=admission.app), base_url="http://testserver", cookies=cookies) as client:
        response = await client.post(DELETION_PATH, headers=admission.headers,
                                     json={"current_password": PASSWORD, "confirmation": "DELETE"})
        invalid_confirmation = await client.post(DELETION_PATH, headers=admission.headers,
                                                 json={"current_password": PASSWORD, "confirmation": "delete"})
    assert response.status_code == 409 and response.json()["detail"]["code"] == "deletion_preview_required"
    assert invalid_confirmation.status_code == 422
    admission.session.add.assert_not_called()
    admission.session.commit.assert_not_awaited()
    assert not admission.app.state.redis.counts


@pytest.mark.asyncio
async def test_acceptance_receipt_recovers_lost_response_without_authentication(admission):
    """A preexisting receipt recovers safe status after session revocation without account data."""
    def save_operation(operation):
        admission.state.operation = operation

    async def commit():
        assert admission.state.owner.account_state == "disabled"
        assert admission.state.operation.account_id == admission.state.owner.id
        assert admission.state.operation.workspace_id == admission.workspace_id
        admission.session.execute.assert_awaited_once()

    admission.session.add.side_effect = save_operation
    admission.session.commit.side_effect = commit
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=admission.app), base_url="http://testserver", cookies=admission.cookies) as client:
        response = await client.post(DELETION_PATH, headers=admission.headers,
                                     json={"current_password": PASSWORD, "confirmation": "DELETE"})
        assert response.status_code == 202
        client.cookies.clear()
        # The receipt was established before POST: a lost response cannot lose this authority.
        client.cookies.set(deletion_routes.RECEIPT_COOKIE, admission.cookies[deletion_routes.RECEIPT_COOKIE])
        recovered = await client.get(f"{DELETION_PATH}-status")
        assert recovered.status_code == 200
        assert recovered.json() == {"status": "queued", "phase": "queued", "error_code": None}
        assert recovered.headers["cache-control"] == "no-store"
        client.cookies.clear()
        unavailable = await client.get(f"{DELETION_PATH}-status")
        assert unavailable.status_code == 404
        client.cookies.set(deletion_routes.RECEIPT_COOKIE, "x" * 43)
        foreign = await client.get(f"{DELETION_PATH}-status")
        assert foreign.status_code == 404
    admission.session.commit.assert_awaited_once()
    deletion_sql = str(admission.session.execute.await_args.args[0].compile(
        dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True},
    ))
    assert deletion_sql == "DELETE FROM auth_session WHERE auth_session.owner_id = 7"
    assert admission.state.operation.receipt_hash == _hash(admission.cookies[deletion_routes.RECEIPT_COOKIE])


@pytest.mark.asyncio
@pytest.mark.parametrize("blocker", ["workspace_has_members", "workspace_has_live_invitations"])
async def test_acceptance_rechecks_new_blocker_under_account_session_workspace_locks(admission, blocker):
    """A blocker introduced after preview is rechecked in ordered lifecycle locks and prevents acceptance."""
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=admission.app), base_url="http://testserver", cookies=admission.cookies) as client:
        preview = await client.get(f"{DELETION_PATH}-preview")
        assert preview.json()["can_delete"]
        if blocker == "workspace_has_members":
            admission.state.member = 99
        else:
            admission.state.invitation = uuid4()
        admission.state.queries.clear()
        response = await client.post(DELETION_PATH, headers=admission.headers,
                                     json={"current_password": PASSWORD, "confirmation": "DELETE"})
    assert response.status_code == 409 and response.json()["detail"]["code"] == blocker
    locked = [query for query in admission.state.queries if query._for_update_arg is not None]
    assert [query.column_descriptions[0]["entity"] for query in locked] == [Owner, AuthSession, Workspace]
    workspace_query = str(locked[-1].compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    assert "workspaces.owner_user_id = 7" in workspace_query
    member_query = next(query for query in admission.state.queries if query.column_descriptions[0]["entity"] is WorkspaceMembership)
    invitation_query = next(query for query in admission.state.queries if query.column_descriptions[0]["entity"] is WorkspaceInvitation)
    member_sql = str(member_query.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    invitation_sql = str(invitation_query.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    assert str(admission.workspace_id) in member_sql and "workspace_memberships.user_id != 7" in member_sql
    assert str(admission.workspace_id) in invitation_sql
    assert all(fragment in invitation_sql for fragment in ("accepted_at IS NULL", "revoked_at IS NULL", "expires_at >"))
    admission.session.add.assert_not_called()
    admission.session.commit.assert_not_awaited()
    assert admission.state.owner.account_state == "active"


@pytest.mark.asyncio
async def test_acceptance_refuses_account_disabled_after_dependency_admission(admission):
    """Dependency admission does not survive disabling the account before the locked recheck."""
    original = admission.session.scalar.side_effect
    async def disable_at_owner_lock(query):
        if query.column_descriptions[0]["entity"] is Owner and query._for_update_arg is not None:
            admission.state.owner.account_state = "disabled"
        return await original(query)
    admission.session.scalar.side_effect = disable_at_owner_lock
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=admission.app), base_url="http://testserver", cookies=admission.cookies) as client:
        response = await client.post(DELETION_PATH, headers=admission.headers,
                                     json={"current_password": PASSWORD, "confirmation": "DELETE"})
    assert response.status_code == 401
    admission.session.add.assert_not_called()
    admission.session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_acceptance_rejects_session_substituted_for_another_owner_after_admission(admission):
    """The locked session must still belong to the authenticated account before deletion proceeds."""
    original = admission.session.scalar.side_effect
    async def substitute_at_session_lock(query):
        if query.column_descriptions[0]["entity"] is AuthSession and query._for_update_arg is not None:
            return SimpleNamespace(owner_id=99, expires_at=datetime.now(UTC) + timedelta(hours=1))
        return await original(query)
    admission.session.scalar.side_effect = substitute_at_session_lock
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=admission.app), base_url="http://testserver", cookies=admission.cookies) as client:
        response = await client.post(DELETION_PATH, headers=admission.headers,
                                     json={"current_password": PASSWORD, "confirmation": "DELETE"})
    assert response.status_code == 401
    admission.session.add.assert_not_called()
    admission.session.commit.assert_not_awaited()
    assert admission.state.owner.account_state == "active"


def worker_harness(phase="graph", cursor=None, account_id=7):
    """Retain one persisted operation across successive worker invocations."""
    operation = SimpleNamespace(id=uuid4(), account_id=account_id, workspace_id=uuid4(),
                                status="queued", phase=phase, cursor=cursor or {}, error_code=None)
    session = MagicMock(commit=AsyncMock(), rollback=AsyncMock(), flush=AsyncMock(),
                        execute=AsyncMock(), delete=AsyncMock())
    session.get = AsyncMock(return_value=operation)
    session.scalar = AsyncMock(return_value=operation)
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    connection = MagicMock(commit=AsyncMock(), execute=AsyncMock(), invalidate=AsyncMock())
    connection.scalar = AsyncMock(side_effect=lambda query, *args: True if "pg_try" in str(query) else 123)
    engine = MagicMock()
    engine.connect.return_value.__aenter__ = AsyncMock(return_value=connection)
    return SimpleNamespace(operation=operation, session=session, connection=connection,
                           ctx={"db_engine": engine, "session_factory": factory})


@pytest.mark.asyncio
@pytest.mark.parametrize("phase,owner,method,error_code", [
    ("graph", "temporal", "erase_account_graph_page", "graph_cleanup_failed"),
    ("storage", "documents", "erase_account_storage_page", "storage_cleanup_failed"),
])
async def test_external_cleanup_failure_is_durable_and_retry_stays_at_failed_stage(monkeypatch, phase, owner, method, error_code):
    """Graph or unlink failures retain original progress and never reach identity finalization."""
    harness = worker_harness(phase, {"page": 3})
    monkeypatch.setattr(deletion_worker, "authorize_account_erasure", AsyncMock())
    monkeypatch.setattr(deletion_cleanup, "authorize_account_erasure", AsyncMock())
    cleanup = AsyncMock(side_effect=OSError("sensitive internal provider or path error"))
    monkeypatch.setattr(getattr(deletion_cleanup, owner), method, cleanup)
    finalizer = AsyncMock()
    monkeypatch.setattr(deletion_worker, "finalize_account_erasure", finalizer)
    assert not await deletion_worker.process_account_deletion(harness.ctx, harness.operation.id)
    assert harness.operation.status == "failed" and harness.operation.phase == phase
    assert harness.operation.cursor == {"page": 3} and harness.operation.error_code == error_code
    assert harness.operation.next_attempt_at > datetime.now(UTC)
    finalizer.assert_not_awaited()
    cleanup.side_effect = None
    cleanup.return_value = (True, {})
    assert await deletion_worker.process_account_deletion(harness.ctx, harness.operation.id)
    assert harness.operation.phase == ("storage" if phase == "graph" else "shared")
    assert harness.operation.error_code is None
    assert harness.operation.status != "succeeded"
    assert cleanup.await_args_list[0].args[2] == cleanup.await_args_list[1].args[2] == {"page": 3}
    finalizer.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancelled_worker_releases_lease_and_resumes_original_durable_cursor(monkeypatch):
    """Cancellation leaves the committed claim resumable and releases its advisory lock."""
    harness = worker_harness("database", {"table": 16})
    monkeypatch.setattr(deletion_worker, "authorize_account_erasure", AsyncMock())
    cleanup = AsyncMock(side_effect=asyncio.CancelledError())
    monkeypatch.setattr(deletion_cleanup, "erase_account_data", cleanup)
    with pytest.raises(asyncio.CancelledError):
        await deletion_worker.process_account_deletion(harness.ctx, harness.operation.id)
    assert harness.operation.status == "running" and harness.operation.cursor == {"table": 16}
    assert "pg_advisory_unlock" in str(harness.connection.execute.await_args.args[0])
    cleanup.side_effect = None
    cleanup.return_value = ErasureProgress("database", {"table": 17})
    assert await deletion_worker.process_account_deletion(harness.ctx, harness.operation.id)
    assert cleanup.await_args.args[2:] == ("database", {"table": 16})
    assert harness.operation.cursor == {"table": 17}
    assert harness.operation.status != "succeeded"


@pytest.mark.asyncio
async def test_foreign_checkpoint_change_cannot_be_finalized(monkeypatch):
    """Reject completion when another execution changed the durable checkpoint during cleanup."""
    harness = worker_harness("verify")
    monkeypatch.setattr(deletion_worker, "authorize_account_erasure", AsyncMock())
    async def change_checkpoint(*args):
        harness.operation.cursor = {"unexpected": True}
        return ErasureProgress("verify", complete=True)
    monkeypatch.setattr(deletion_cleanup, "erase_account_data", change_checkpoint)
    finalizer = AsyncMock()
    monkeypatch.setattr(deletion_worker, "finalize_account_erasure", finalizer)
    assert not await deletion_worker.process_account_deletion(harness.ctx, harness.operation.id)
    finalizer.assert_not_awaited()
    assert harness.operation.status != "succeeded"


@pytest.mark.asyncio
@pytest.mark.parametrize("account_id", [1, 7])
async def test_finalization_erases_identity_last_and_keeps_only_sanitized_bootstrap_tombstone(monkeypatch, account_id):
    """Verify finalizer ordering, account-scoped identity removal and seven-day sanitized receipts."""
    from core.workspaces import erasure

    harness = worker_harness("verify", {"sensitive": "cursor"}, account_id=account_id)
    owner = Owner(id=account_id, password_hash="old-hash", account_state="disabled", email="old@example.test",
                  default_workspace_id=harness.operation.workspace_id, email_verified_at=datetime.now(UTC),
                  email_verification_source="google_oidc", created_at=datetime.now(UTC))
    harness.session.get.return_value = owner
    monkeypatch.setattr(deletion_worker, "authorize_account_erasure", AsyncMock())
    async def workspace_cleanup(session, scope):
        assert owner.default_workspace_id is None
        assert harness.session.flush.await_count == 1
        assert harness.operation.account_id == account_id
    workspace_finalizer = AsyncMock(side_effect=workspace_cleanup)
    monkeypatch.setattr(erasure, "finalize_account_workspace_erasure", workspace_finalizer)
    await deletion_worker.finalize_account_erasure(harness.session, harness.operation)
    assert harness.operation.status == "succeeded" and harness.operation.phase == "complete"
    assert harness.operation.account_id is None and harness.operation.workspace_id is None and harness.operation.cursor == {}
    assert harness.operation.expires_at - harness.operation.completed_at == timedelta(days=7)
    sql = [str(call.args[0].compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
           for call in harness.session.execute.await_args_list]
    assert len(sql) == 2 and all(f"owner_id = {account_id}" in query for query in sql)
    if account_id == 1:
        harness.session.delete.assert_not_awaited()
        assert owner.account_state == "disabled" and owner.password_hash == ""
        assert owner.email is owner.email_verified_at is owner.email_verification_source is None
        assert owner.created_at == datetime(1970, 1, 1, tzinfo=UTC)
        harness.session.scalar.return_value = owner
        assert await get_active_account(harness.session, 1, multi_workspace_enabled=True) is None
    else:
        harness.session.delete.assert_awaited_once_with(owner)


@pytest.mark.asyncio
async def test_disabled_bootstrap_tombstone_blocks_first_run_setup(admission, monkeypatch):
    """A retained bootstrap row keeps setup closed and its primary-key conflict cannot replace it."""
    tombstone = Owner(id=1, password_hash="", account_state="disabled", email=None, default_workspace_id=None)
    admission.state.owner = tombstone
    duplicate = RuntimeError("duplicate primary key")
    duplicate.sqlstate, duplicate.constraint_name = "23505", "owner_pkey"
    admission.session.flush.side_effect = IntegrityError("insert owner", {}, duplicate)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=admission.app), base_url="http://testserver", cookies=admission.cookies) as client:
        status = await client.get("/api/v1/auth/setup-status")
        assert status.json() == {"setupRequired": False}
        response = await client.post("/api/v1/auth/setup", json={"password": PASSWORD},
                                     headers={**admission.headers, "X-Setup-Token": "unit-only-setup-token"})
    assert response.status_code == 409
    admission.session.rollback.assert_awaited_once()
    admission.session.commit.assert_not_awaited()
    assert tombstone.account_state == "disabled" and tombstone.password_hash == ""


@pytest.mark.asyncio
async def test_peer_workspace_reference_cleanup_is_bounded_to_departing_identity(monkeypatch):
    """Peer cleanup removes the departing identity's links without deleting peer canonical content."""
    from core.workspaces import erasure

    scope = AccountErasureScope(uuid4(), 7, uuid4())
    session = MagicMock(scalar=AsyncMock(return_value="departing@example.test"), execute=AsyncMock())
    authorization = AsyncMock()
    monkeypatch.setattr(erasure, "authorize_account_erasure", authorization)
    await erasure.erase_account_workspace_references(session, scope)
    authorization.assert_awaited_once_with(session, scope)
    statements = [str(call.args[0].compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
                  for call in session.execute.await_args_list]
    assert statements == [
        "DELETE FROM workspace_shares WHERE workspace_shares.member_user_id = 7",
        f"DELETE FROM workspace_memberships WHERE workspace_memberships.user_id = 7 AND workspace_memberships.workspace_id != '{scope.workspace_id}'",
        "DELETE FROM workspace_invitations WHERE workspace_invitations.accepted_by_user_id = 7",
        "DELETE FROM workspace_invitations WHERE workspace_invitations.email = 'departing@example.test'",
    ]
