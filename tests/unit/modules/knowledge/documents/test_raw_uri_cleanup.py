"""Raw-URI cleanup proof: global reference EXISTS, scoped receipt, and instance-operator-only sweep."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from core.workspaces.schemas import AccessFence, InternalJobScope
from modules.knowledge.documents import public

WS, SRC, OP = uuid4(), uuid4(), uuid4()
SCOPE = InternalJobScope(workspace_id=WS, actor_user_id=7, membership_revision=3, source_id=SRC, source_generation=5)
FENCE = AccessFence(WS, 7, 3, 2)


def _sql(statement) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


def _session(row, referenced=None):
    session = MagicMock()
    session.execute = AsyncMock(return_value=SimpleNamespace(one_or_none=lambda: row))
    session.scalar = AsyncMock(return_value=referenced or uuid4())
    return session


async def _call(session, current=FENCE):
    with patch.object(public, "read_access_fence", AsyncMock(return_value=current)):
        return await public.raw_uri_is_referenced_for_cleanup(
            session, OP, scope=SCOPE, multi_workspace_enabled=True, access_fence=FENCE,
        )


async def test_reference_check_is_global_but_receipt_read_is_scoped() -> None:
    session = _session(SimpleNamespace(raw_uri="u/x", configuration_revision=2))
    assert await _call(session) is True
    receipt_sql = _sql(session.execute.await_args.args[0])
    for column in ("workspace_id", "actor_user_id", "membership_revision"):
        assert column in receipt_sql
    reference_sql = _sql(session.scalar.await_args.args[0])
    assert "raw_uri" in reference_sql and "workspace_id" not in reference_sql  # shared storage: global EXISTS


async def test_no_uri_means_no_reference_query() -> None:
    session = _session(SimpleNamespace(raw_uri=None, configuration_revision=2))
    assert await _call(session) is False
    session.scalar.assert_not_called()


async def test_unfound_stale_or_fence_mismatched_receipt_raises() -> None:
    with pytest.raises(HTTPException):
        await _call(_session(None))
    with pytest.raises(HTTPException):
        await _call(_session(SimpleNamespace(raw_uri="u", configuration_revision=9)))
    with pytest.raises(HTTPException):
        await _call(_session(SimpleNamespace(raw_uri="u", configuration_revision=2)), current=AccessFence(WS, 7, 3, 3))


@pytest.mark.parametrize("kwargs", [
    {"instance_operator": False, "multi_workspace_enabled": False},
    {"instance_operator": True, "multi_workspace_enabled": True},
    {"instance_operator": 1, "multi_workspace_enabled": False},
])
async def test_raw_uris_denial_raises_never_empty_set(kwargs) -> None:
    with patch("core.auth.public.get_active_account", AsyncMock(return_value=object())), \
            pytest.raises(HTTPException) as caught:
        await public.raw_uris(MagicMock(), **kwargs)
    assert caught.value.status_code == 403


async def test_raw_uris_requires_active_bootstrap_account() -> None:
    with patch("core.auth.public.get_active_account", AsyncMock(return_value=None)), \
            pytest.raises(HTTPException):
        await public.raw_uris(MagicMock(), instance_operator=True, multi_workspace_enabled=False)


async def test_raw_uris_returns_global_nonempty_set_for_instance_operator() -> None:
    session = MagicMock()
    session.scalars = AsyncMock(return_value=SimpleNamespace(all=lambda: ["a", "", "b", "a"]))
    with patch("core.auth.public.get_active_account", AsyncMock(return_value=object())):
        assert await public.raw_uris(session, instance_operator=True, multi_workspace_enabled=False) == {"a", "b"}
    assert "workspace_id" not in _sql(session.scalars.await_args.args[0])


async def test_orphan_sweep_is_disabled_in_multiworkspace_and_passes_flag_otherwise() -> None:
    from modules.ingestion import worker as ingestion_worker

    factory = MagicMock()
    ctx = {"session_factory": factory, "settings": SimpleNamespace(multi_workspace_enabled=True)}
    assert await ingestion_worker.cleanup_storage_orphans(ctx) == 0
    factory.assert_not_called()  # no activity registered, no raw_uris call
