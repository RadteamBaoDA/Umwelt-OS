"""Host-only bootstrap recovery rejects residue and commits only one new identity unit."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from core.auth import recovery
from scripts import account_deletion


def tombstone(**changes):
    fields = {"id": 1, "account_state": "disabled", "password_hash": "", "email": None,
              "default_workspace_id": None, "email_verified_at": None,
              "email_verification_source": None, "created_at": datetime(1970, 1, 1, tzinfo=UTC)}
    return SimpleNamespace(**(fields | changes))


@pytest.fixture
def recovery_context(monkeypatch):
    session = MagicMock(get=AsyncMock(return_value=tombstone()), scalar=AsyncMock(return_value=False),
                        flush=AsyncMock(), commit=AsyncMock())
    lock = AsyncMock()
    provision = AsyncMock(return_value=uuid4())
    monkeypatch.setattr(recovery, "lock_account_lifecycle", lock)
    monkeypatch.setattr(recovery, "provision_default_workspace_in_uow", provision)
    monkeypatch.setattr(recovery, "hash_password", lambda _password: "new-hash")
    return session, lock, provision


@pytest.mark.asyncio
async def test_recovery_creates_only_fresh_workspace_under_lifecycle_lock(recovery_context):
    session, lock, provision = recovery_context
    owner = session.get.return_value
    await recovery.recover_bootstrap_account_in_uow(session, "new-private-password")
    lock.assert_awaited_once_with(session, (1,))
    provision.assert_awaited_once_with(session, 1)
    assert owner.account_state == "active"
    assert owner.default_workspace_id == provision.return_value
    assert owner.password_hash == "new-hash" and owner.email is None
    assert owner.created_at > datetime(1970, 1, 1, tzinfo=UTC)
    session.add.assert_not_called()  # Provisioning owns only its new workspace/membership.
    session.commit.assert_not_awaited()  # Caller owns the identity/workspace transaction.


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"account_state": "active"}, {"password_hash": "old-hash"}, {"email": "old@example.test"},
    {"default_workspace_id": uuid4()}, {"email_verified_at": datetime.now(UTC)},
    {"email_verification_source": "google_oidc"}, {"created_at": datetime.now(UTC)},
])
async def test_recovery_refuses_non_sanitized_or_active_account(recovery_context, changes):
    session, _lock, provision = recovery_context
    session.get.return_value = tombstone(**changes)
    with pytest.raises(ValueError, match="sanitized"):
        await recovery.recover_bootstrap_account_in_uow(session, "new-private-password")
    provision.assert_not_awaited()
    session.flush.assert_not_awaited()


@pytest.mark.asyncio
async def test_recovery_refuses_pending_operation_or_domain_residue(recovery_context):
    session, _lock, provision = recovery_context
    session.scalar.return_value = True
    with pytest.raises(ValueError, match="incomplete"):
        await recovery.recover_bootstrap_account_in_uow(session, "new-private-password")
    query = str(session.scalar.call_args.args[0].compile(dialect=postgresql.dialect(),
                                                       compile_kwargs={"literal_binds": True}))
    for required in ("account_deletion_operations.account_id = 1", "workspaces.owner_user_id = 1",
                     "auth_session.owner_id = 1", "google_identity.owner_id = 1",
                     "chat_todos.actor_user_id = 1", "workspace_shares.member_user_id = 1"):
        assert required in query
    assert "github_oauth_grants.github_user_id = 1" not in query
    provision.assert_not_awaited()
    assert session.get.return_value.account_state == "disabled"


@pytest.mark.asyncio
async def test_recovery_rejects_missing_owner_or_invalid_password(recovery_context):
    session, lock, provision = recovery_context
    with pytest.raises(ValueError, match="12 to 128"):
        await recovery.recover_bootstrap_account_in_uow(session, "short")
    lock.assert_not_awaited()
    session.get.return_value = None
    with pytest.raises(ValueError, match="sanitized"):
        await recovery.recover_bootstrap_account_in_uow(session, "new-private-password")
    provision.assert_not_awaited()


@pytest.mark.asyncio
async def test_workspace_provision_failure_keeps_tombstone_disabled(recovery_context):
    session, _lock, provision = recovery_context
    owner = session.get.return_value
    provision.side_effect = RuntimeError("workspace creation failed")
    with pytest.raises(RuntimeError, match="workspace creation"):
        await recovery.recover_bootstrap_account_in_uow(session, "new-private-password")
    assert owner.account_state == "disabled" and owner.default_workspace_id is None
    assert owner.password_hash == ""
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_cli_recovery_failure_exits_session_without_commit(monkeypatch, capsys):
    session = MagicMock(commit=AsyncMock())
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=session)
    context.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=context)
    engine = MagicMock(dispose=AsyncMock())
    monkeypatch.setattr(account_deletion, "Settings", lambda: SimpleNamespace(database_url="unused"))
    monkeypatch.setattr(account_deletion, "make_session_factory", lambda _url: (engine, factory))
    monkeypatch.setattr(account_deletion, "recover_bootstrap_account_in_uow",
                        AsyncMock(side_effect=RuntimeError("provision failed")))
    with pytest.raises(RuntimeError, match="provision failed"):
        await account_deletion.run("recover-bootstrap", None, password="secret-never-log")
    session.commit.assert_not_awaited()
    context.__aexit__.assert_awaited_once()
    engine.dispose.assert_awaited_once()
    assert "secret-never-log" not in capsys.readouterr().out


@pytest.mark.asyncio
async def test_cli_commits_recovery_once_without_printing_password(monkeypatch, capsys):
    session = MagicMock(commit=AsyncMock())
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=session)
    factory = MagicMock(return_value=context)
    engine = MagicMock(dispose=AsyncMock())
    monkeypatch.setattr(account_deletion, "Settings", lambda: SimpleNamespace(database_url="unused"))
    monkeypatch.setattr(account_deletion, "make_session_factory", lambda _url: (engine, factory))
    recover = AsyncMock()
    monkeypatch.setattr(account_deletion, "recover_bootstrap_account_in_uow", recover)
    await account_deletion.run("recover-bootstrap", None, password="secret-never-log")
    recover.assert_awaited_once_with(session, "secret-never-log")
    session.commit.assert_awaited_once()
    assert "secret-never-log" not in capsys.readouterr().out


def test_cli_refuses_piped_password_before_reading(monkeypatch):
    monkeypatch.setattr(account_deletion.sys, "argv", ["account_deletion", "recover-bootstrap", "--confirm-new-workspace"])
    monkeypatch.setattr(account_deletion.sys.stdin, "isatty", lambda: False)
    password_prompt = MagicMock()
    monkeypatch.setattr(account_deletion, "getpass", password_prompt)
    with pytest.raises(SystemExit, match="2"):
        account_deletion.main()
    password_prompt.assert_not_called()


def test_cli_does_not_print_database_exception_parameters(monkeypatch, capsys):
    monkeypatch.setattr(account_deletion.sys, "argv", ["account_deletion", "status"])
    monkeypatch.setattr(account_deletion, "run", AsyncMock(side_effect=RuntimeError("credential-hash SQL parameters")))
    with pytest.raises(SystemExit, match="1"):
        account_deletion.main()
    output = capsys.readouterr()
    assert "credential-hash" not in output.err and "credential-hash" not in output.out
