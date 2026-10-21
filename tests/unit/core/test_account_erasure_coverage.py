"""Account-erasure coverage and peer-scope checks without a live database."""

from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
from sqlalchemy.dialects import postgresql

from core.auth.deletion_cleanup import (
    _CORE_TABLES,
    _OWNERS,
    _SPECIAL_TABLES,
    account_erasure_tables,
)
from core.auth.erasure_contracts import AccountErasureScope
from core.database import Base
from core.erasure_sql import account_row_predicate


@pytest.mark.asyncio
async def test_database_cleanup_skips_empty_tables_in_bounded_batches(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.auth import deletion_cleanup

    class SessionContext:
        async def __aenter__(self):
            return MagicMock(commit=AsyncMock(), rollback=AsyncMock())

        async def __aexit__(self, *_args):
            return None

    names = tuple(f"table_{index}" for index in range(18))
    owner = MagicMock()
    owner.erase_page = AsyncMock(return_value=0)
    monkeypatch.setattr(deletion_cleanup, "authorize_account_erasure", AsyncMock())
    monkeypatch.setattr(deletion_cleanup, "account_erasure_tables", lambda: names)
    monkeypatch.setattr(deletion_cleanup, "_table_owner", lambda _name: owner)

    class Factory:
        def __call__(self):
            return SessionContext()

    scope = AccountErasureScope(UUID(int=1), 7, UUID(int=2))
    first = await deletion_cleanup.erase_account_data(
        {"session_factory": Factory()}, scope, "database", {"table": 0},
    )
    assert first.phase == "database" and first.cursor == {"table": 16}
    assert owner.erase_page.await_count == 16

    second = await deletion_cleanup.erase_account_data(
        {"session_factory": Factory()}, scope, first.phase, first.cursor,
    )
    assert second.phase == "references"
    assert owner.erase_page.await_count == 18


@pytest.mark.asyncio
async def test_reference_stage_anonymizes_settings_before_committing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The coordinator invokes the Settings owner contract before advancing its checkpoint."""
    from core.auth import deletion_cleanup

    session = MagicMock(commit=AsyncMock(), rollback=AsyncMock())

    class SessionContext:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *_args):
            return None

    settings_cleanup = AsyncMock()
    workspace_cleanup = AsyncMock()
    monkeypatch.setattr(deletion_cleanup, "authorize_account_erasure", AsyncMock())
    monkeypatch.setattr(deletion_cleanup.settings, "erase_account_settings_references", settings_cleanup)
    monkeypatch.setattr(deletion_cleanup, "erase_account_workspace_references", workspace_cleanup)
    scope = AccountErasureScope(UUID(int=1), 1, UUID(int=2))
    result = await deletion_cleanup.erase_account_data(
        {"session_factory": SessionContext}, scope, "references", {},
    )
    assert result.phase == "verify" and not result.complete
    settings_cleanup.assert_awaited_once_with(session, scope)
    workspace_cleanup.assert_awaited_once_with(session, scope)
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_retained_settings_updater_prevents_completion(monkeypatch: pytest.MonkeyPatch) -> None:
    """A residue in a peer workspace must block success, even for bootstrap account 1."""
    from core.auth import deletion_cleanup

    class SessionContext:
        async def __aenter__(self):
            return MagicMock(rollback=AsyncMock())

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr(deletion_cleanup, "authorize_account_erasure", AsyncMock())
    monkeypatch.setattr(deletion_cleanup, "account_erasure_tables", lambda: ())
    monkeypatch.setattr(deletion_cleanup.connectors, "verify_account_shared_rows", AsyncMock())
    monkeypatch.setattr(deletion_cleanup.translations, "verify_account_shared_rows", AsyncMock())
    monkeypatch.setattr(deletion_cleanup.settings, "verify_account_settings_references", AsyncMock(
        side_effect=RuntimeError("Account settings updater reference remains"),
    ))
    with pytest.raises(RuntimeError, match="updater reference remains"):
        await deletion_cleanup.erase_account_data(
            {"session_factory": SessionContext}, AccountErasureScope(UUID(int=1), 1, UUID(int=2)),
            "verify", {},
        )


def test_every_registered_table_has_one_erasure_owner_or_finalizer() -> None:
    ordinary = set(account_erasure_tables())
    declared = list(_CORE_TABLES) + [name for owner in _OWNERS for name in owner.ERASURE_TABLES]
    finalizer = {
        "owner", "auth_session", "google_identity", "account_deletion_operations",
        "workspaces", "workspace_memberships", "workspace_invitations", "workspace_shares",
        "remote_heavy_guards", "maintenance_summary",
    }
    assert len(declared) == len(set(declared))
    assert ordinary | _SPECIAL_TABLES | finalizer == set(Base.metadata.tables)
    assert ordinary.isdisjoint(_SPECIAL_TABLES)


def test_peer_workspace_author_survives_only_for_other_users() -> None:
    scope = AccountErasureScope(UUID(int=1), 7, UUID(int=2))
    table = Base.metadata.tables["chat_todos"]
    query = str(account_row_predicate(table, scope).compile(
        dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True},
    ))
    assert "chat_todos.workspace_id = '00000000-0000-0000-0000-000000000002'" in query
    assert "chat_todos.actor_user_id = 7" in query
    document = str(account_row_predicate(Base.metadata.tables["documents"], scope).compile(
        dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True},
    ))
    assert "actor_user_id" not in document
