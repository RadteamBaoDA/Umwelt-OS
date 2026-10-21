from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest


@pytest.mark.parametrize('exclusive', [False, True])
@pytest.mark.asyncio
async def test_storage_writer_and_cleanup_use_conflicting_session_leases(exclusive: bool):
    from core.storage_admission import workspace_storage_lease

    connection = AsyncMock()
    connection.scalar.return_value = True
    engine = MagicMock()
    engine.connect.return_value.__aenter__ = AsyncMock(return_value=connection)
    factory = SimpleNamespace(kw={'bind': engine})
    async with workspace_storage_lease(factory, uuid4(), exclusive=exclusive):
        assert connection.commit.await_count == 1  # no transaction held through file I/O
    acquire = str(connection.scalar.await_args.args[0])
    release = str(connection.execute.await_args.args[0])
    assert ('_shared' in acquire) is (not exclusive)
    assert ('_shared' in release) is (not exclusive)
    assert connection.commit.await_count == 2


@pytest.mark.asyncio
async def test_storage_cleanup_never_proceeds_while_a_writer_holds_its_lease():
    from core.storage_admission import WorkspaceStorageBusy, workspace_storage_lease

    connection = AsyncMock()
    connection.scalar.return_value = False
    engine = MagicMock()
    engine.connect.return_value.__aenter__ = AsyncMock(return_value=connection)
    with pytest.raises(WorkspaceStorageBusy):
        async with workspace_storage_lease(SimpleNamespace(kw={'bind': engine}), uuid4(), exclusive=True):
            pytest.fail('Cleanup entered while an upload was active')
    connection.execute.assert_not_awaited()
