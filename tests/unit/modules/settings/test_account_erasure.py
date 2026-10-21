"""Exercise updater anonymization against an in-memory SQL table without service calls."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
from sqlalchemy import create_engine, insert, select
from sqlalchemy.orm import Session

from core.auth.erasure_contracts import AccountErasureScope
from modules.settings import erasure
from modules.settings.models import TranslationSettingsRecord


@pytest.mark.asyncio
@pytest.mark.parametrize("account_id", [1, 7])
async def test_settings_anonymizes_departing_updater_and_preserves_peer_choices(
    monkeypatch: pytest.MonkeyPatch, account_id: int,
) -> None:
    """Both bootstrap tombstones and deleted identities lose references in surviving rows."""
    authority = AsyncMock()
    monkeypatch.setattr(erasure, "authorize_account_erasure", authority)
    engine = create_engine("sqlite://")
    TranslationSettingsRecord.__table__.create(engine)
    with Session(engine) as sql:
        sql.execute(insert(TranslationSettingsRecord), [
            {
                "workspace_id": UUID(int=index), "updated_by_user_id": updater,
                "enabled": enabled, "target_language": language, "configuration_revision": 8,
                "created_at": datetime(2025, 1, 1, tzinfo=UTC),
                "updated_at": datetime(2025, 2, 1, tzinfo=UTC),
            }
            for index, updater, enabled, language in [
                (11, account_id, False, "en"), (12, account_id, True, "vi"), (13, 9, True, "en"),
            ]
        ])
        sql.commit()
        table = TranslationSettingsRecord.__table__
        before = [dict(row) for row in sql.execute(select(table).order_by(table.c.workspace_id)).mappings()]
        session = MagicMock(execute=AsyncMock(side_effect=sql.execute), scalar=AsyncMock(side_effect=sql.scalar))
        scope = AccountErasureScope(UUID(int=21), account_id, UUID(int=11))
        with pytest.raises(RuntimeError, match="updater reference remains"):
            await erasure.verify_account_settings_references(session, scope)
        await erasure.erase_account_settings_references(session, scope)
        await erasure.erase_account_settings_references(session, scope)
        await erasure.verify_account_settings_references(session, scope)
        after = [dict(row) for row in sql.execute(select(table).order_by(table.c.workspace_id)).mappings()]
        expected = [
            {**row, "updated_by_user_id": None} if row["updated_by_user_id"] == account_id else row
            for row in before
        ]
        assert after == expected
        assert authority.await_count == 4
        session.commit.assert_not_called()
    engine.dispose()


@pytest.mark.asyncio
async def test_settings_reference_cleanup_requires_durable_deletion_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Constructing a scope alone cannot modify or inspect peer settings."""
    monkeypatch.setattr(erasure, "authorize_account_erasure", AsyncMock(side_effect=PermissionError))
    session = MagicMock(execute=AsyncMock(), scalar=AsyncMock())
    scope = AccountErasureScope(UUID(int=21), 1, UUID(int=11))
    for operation in (erasure.erase_account_settings_references, erasure.verify_account_settings_references):
        with pytest.raises(PermissionError):
            await operation(session, scope)
    session.execute.assert_not_awaited()
    session.scalar.assert_not_awaited()
