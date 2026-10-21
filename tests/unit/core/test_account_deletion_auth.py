"""Account deletion admission, scope, and receipt safety without live database calls."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException, Response

from core.auth.deletion_models import AccountDeletionOperation
from core.auth.deletion_routes import accept_account_deletion, preview_account_deletion
from core.auth.deletion_schemas import DeletionRequest
from core.auth.deletion_service import authorize_account_erasure, receipt_operation
from core.auth.deletion_worker import process_account_deletion
from core.auth.erasure_contracts import AccountErasureScope, ErasureProgress
from scripts import account_deletion as host_deletion


@pytest.mark.asyncio
async def test_erasure_authority_requires_original_owned_workspace() -> None:
    workspace_id, foreign_workspace_id = uuid4(), uuid4()
    scope = AccountErasureScope(uuid4(), 7, workspace_id)
    session = MagicMock()
    session.scalar = AsyncMock(side_effect=[SimpleNamespace(status="running"), SimpleNamespace(account_state="disabled")])
    with (patch("core.auth.deletion_service.account_deletion_blockers", new_callable=AsyncMock,
                return_value=(foreign_workspace_id, [])), pytest.raises(PermissionError)):
        await authorize_account_erasure(session, scope)


@pytest.mark.asyncio
async def test_expired_receipt_does_not_authorize_status() -> None:
    session = MagicMock()
    session.scalar = AsyncMock(return_value=SimpleNamespace(expires_at=datetime.now(UTC) - timedelta(seconds=1)))
    assert await receipt_operation(session, "digest") is None


@pytest.mark.asyncio
async def test_preview_replaces_other_accounts_receipt() -> None:
    old_token = "a" * 43
    request = MagicMock()
    request.cookies = {"bbd_account_deletion": old_token}
    request.app.state.settings.secure_cookies = True
    response = Response()
    session = MagicMock()
    with (patch("core.auth.deletion_routes.receipt_operation", new_callable=AsyncMock,
                return_value=SimpleNamespace(account_id=99)),
          patch("core.auth.deletion_routes.account_deletion_blockers", new_callable=AsyncMock,
                return_value=(uuid4(), []))):
        preview = await preview_account_deletion(request, response, SimpleNamespace(owner_id=7), session)
    assert preview.can_delete
    assert response.headers["set-cookie"].startswith("bbd_account_deletion=")
    assert old_token not in response.headers["set-cookie"]
    assert "httponly" in response.headers["set-cookie"].lower()


@pytest.mark.asyncio
async def test_accept_rechecks_password_before_disabling_account() -> None:
    request = MagicMock()
    request.cookies = {"bbd_account_deletion": "a" * 43}
    request.app.state.settings.secure_cookies = True
    response = Response()
    session = MagicMock()
    session.commit = AsyncMock()
    session.execute = AsyncMock()
    owner = SimpleNamespace(id=7, password_hash="stored", account_state="active")
    with (patch("core.auth.deletion_routes._allow_attempt", new_callable=AsyncMock),
          patch("core.auth.deletion_routes._lock_owner_session", new_callable=AsyncMock,
                return_value=(owner, SimpleNamespace())),
          patch("core.auth.deletion_routes.verify_password", return_value=False),
          pytest.raises(HTTPException) as error):
        await accept_account_deletion(DeletionRequest(current_password="wrong", confirmation="DELETE"),
                                      request, response, SimpleNamespace(owner_id=7), session, MagicMock(), "csrf")
    assert error.value.status_code == 400
    assert owner.account_state == "active"
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_accept_disables_and_revokes_in_one_commit() -> None:
    request = MagicMock()
    request.cookies = {"bbd_account_deletion": "a" * 43}
    request.app.state.settings.secure_cookies = True
    response = Response()
    session = MagicMock()
    session.commit = AsyncMock()
    session.execute = AsyncMock()
    owner = SimpleNamespace(id=7, password_hash="stored", account_state="active")
    with (patch("core.auth.deletion_routes._allow_attempt", new_callable=AsyncMock),
          patch("core.auth.deletion_routes._lock_owner_session", new_callable=AsyncMock,
                return_value=(owner, SimpleNamespace())),
          patch("core.auth.deletion_routes.verify_password", return_value=True),
          patch("core.auth.deletion_routes.account_deletion_blockers", new_callable=AsyncMock,
                return_value=(uuid4(), [])),
          patch("core.auth.deletion_routes.receipt_operation", new_callable=AsyncMock, return_value=None)):
        result = await accept_account_deletion(DeletionRequest(current_password="correct", confirmation="DELETE"),
                                               request, response, SimpleNamespace(owner_id=7), session, MagicMock(), "csrf")
    assert result.status == "queued"
    assert owner.account_state == "disabled"
    operation = session.add.call_args.args[0]
    assert isinstance(operation, AccountDeletionOperation)
    assert operation.phase == "queued"
    session.execute.assert_awaited_once()
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_host_status_redacts_identifiers_and_retry_requires_failure(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    operation = SimpleNamespace(id=uuid4(), status="failed", phase="external", error_code="cleanup_retry_required",
                                receipt_hash="secret-receipt", account_id=7, workspace_id=uuid4())
    session = MagicMock()
    session.scalars = AsyncMock(return_value=SimpleNamespace(all=lambda: [operation]))
    session.commit = AsyncMock()
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    engine = MagicMock()
    engine.dispose = AsyncMock()
    monkeypatch.setattr(host_deletion, "Settings", lambda: SimpleNamespace(database_url="unused"))
    monkeypatch.setattr(host_deletion, "make_session_factory", lambda _url: (engine, factory))
    await host_deletion.run("status", operation.id)
    output = capsys.readouterr().out
    assert str(operation.id) in output
    assert "secret-receipt" not in output and str(operation.workspace_id) not in output and '"account_id"' not in output
    await host_deletion.run("retry", operation.id)
    session.commit.assert_awaited_once()
    operation.status = "succeeded"
    with pytest.raises(SystemExit):
        await host_deletion.run("retry", operation.id)


@pytest.mark.asyncio
async def test_worker_does_not_checkpoint_after_advisory_connection_changes() -> None:
    operation = SimpleNamespace(id=uuid4(), account_id=7, workspace_id=uuid4(),
                                status="queued", phase="queued", cursor={}, error_code=None)
    session = MagicMock()
    session.get = AsyncMock(return_value=operation)
    session.commit = AsyncMock()
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    connection = MagicMock()
    connection.scalar = AsyncMock(side_effect=[123, True, 456])
    connection.commit = AsyncMock()
    connection.execute = AsyncMock()
    engine = MagicMock()
    engine.connect.return_value.__aenter__ = AsyncMock(return_value=connection)
    with (patch("core.auth.deletion_worker.authorize_account_erasure", new_callable=AsyncMock),
          patch("core.auth.deletion_cleanup.erase_account_data", new_callable=AsyncMock,
                return_value=ErasureProgress("auxiliary"))):
        result = await process_account_deletion({"db_engine": engine, "session_factory": factory}, operation.id)
    assert result is False
    assert operation.status == "failed"
    assert operation.phase == "queued"
    assert session.commit.await_count == 2  # running claim and safe failure; no progress checkpoint
