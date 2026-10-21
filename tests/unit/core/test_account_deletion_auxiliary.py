from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from core.auth.erasure_contracts import AccountErasureScope


@pytest.mark.asyncio
async def test_redis_erasure_is_scoped_bounded_and_preserves_peer_keys():
    from core.auth.deletion_auxiliary import erase_auxiliary_data

    scope = AccountErasureScope(uuid4(), 7, uuid4())
    session = AsyncMock()
    session.scalar.return_value = 'me@example.test'
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=None)
    mine = f'cache:memory:active:{scope.workspace_id}'
    peer = f'cache:memory:active:{uuid4()}'
    scoped_search = f'chat:websearch:{scope.workspace_id}:7:2026-10-19'.encode()
    peer_search = f'chat:websearch:{scope.workspace_id}:70:2026-10-19'.encode()
    redis = AsyncMock()
    redis.scan.return_value = (123, [mine.encode(), peer.encode(), scoped_search, peer_search,
                                   b'chat:websearch:7:2026-10-19', b'chat:websearch:70:2026-10-19'])
    with patch('core.auth.deletion_auxiliary.authorize_account_erasure', new_callable=AsyncMock):
        done, cursor = await erase_auxiliary_data({'session_factory': factory, 'redis': redis}, scope, {'checkpoints': True})
    assert not done and cursor == {'checkpoints': True, 'redis': 123}
    redis.scan.assert_awaited_once_with(cursor=0, count=200)
    assert set(redis.delete.await_args.args) == {mine.encode(), scoped_search, b'chat:websearch:7:2026-10-19'}
    session.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_redis_failure_never_reports_auxiliary_erasure_complete():
    from core.auth.deletion_auxiliary import erase_auxiliary_data

    scope = AccountErasureScope(uuid4(), 7, uuid4())
    session = AsyncMock()
    session.scalar.return_value = None
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=None)
    redis = AsyncMock()
    redis.scan.side_effect = OSError('unavailable')
    with patch('core.auth.deletion_auxiliary.authorize_account_erasure', new_callable=AsyncMock), pytest.raises(OSError):
        await erase_auxiliary_data({'session_factory': factory, 'redis': redis}, scope, {'checkpoints': True})


@pytest.mark.asyncio
async def test_checkpoint_deletion_waits_for_the_active_agent_lease():
    from modules.agents.account_checkpoints import erase_account_checkpoints_page

    scope = AccountErasureScope(uuid4(), 7, uuid4())
    session = AsyncMock()
    session.scalar.return_value = True
    rows = MagicMock()
    rows.all.return_value = [SimpleNamespace(id=uuid4(), checkpoint_thread_id=str(uuid4()))]
    session.scalars.return_value = rows
    with patch('modules.agents.account_checkpoints.authorize_account_erasure', new_callable=AsyncMock), patch('modules.agents.account_checkpoints.try_agent_run_lease_in_uow', new_callable=AsyncMock, return_value=False):
        assert not await erase_account_checkpoints_page(session, scope)
    session.execute.assert_not_awaited()
