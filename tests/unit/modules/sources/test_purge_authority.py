"""Source purge retained-authority tests (D2a-1): capture, resolvers, coverage, worker admission."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

from core.workspaces.schemas import AccessFence, InternalJobScope, WorkspaceContext
from modules.knowledge.documents.schemas import (
    DocumentCleanupJobIdentity,
    DocumentCleanupPreparationLimitError,
)
from modules.sources import public, worker
from modules.sources.schemas import SourcePurgeJobIdentity

WS, SRC, OP = uuid4(), uuid4(), uuid4()


def _sql(statement) -> str:
    return str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


def _bound(statement) -> tuple[str, list[object]]:
    compiled = statement.compile(dialect=postgresql.dialect())
    return str(compiled), list(compiled.params.values())


def _fence(config: int = 7, membership: int = 3) -> AccessFence:
    return AccessFence(WS, 11, membership, config)


def _scope() -> InternalJobScope:
    return InternalJobScope(WS, 11, 3, SRC, 2)


def _row(config: int | None = 7, **extra):
    values = {"id": OP, "workspace_id": WS, "actor_user_id": 11, "membership_revision": 3,
              "configuration_revision": config, "source_id": SRC, "generation": 2}
    return SimpleNamespace(**{**values, **extra})


def _session(row=None):
    session = MagicMock()
    result = MagicMock()
    result.one_or_none.return_value = row
    session.execute = AsyncMock(return_value=result)
    session.scalar = AsyncMock(return_value=None)
    session.scalars = AsyncMock(return_value=MagicMock(all=list))
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.refresh = AsyncMock()
    return session


def _identity(**overrides) -> SourcePurgeJobIdentity:
    values = {"operation_id": OP, "workspace_id": WS, "actor_user_id": 11, "membership_revision": 3,
              "configuration_revision": 7, "source_id": SRC, "source_generation": 2}
    return SourcePurgeJobIdentity(**{**values, **overrides})


# --- contracts ---------------------------------------------------------------------------------

@pytest.mark.parametrize("field", ["actor_user_id", "membership_revision", "configuration_revision", "source_generation"])
@pytest.mark.parametrize("bad", [0, -1, True])
def test_source_identity_rejects_zero_negative_and_bool_epochs(field, bad) -> None:
    with pytest.raises(ValidationError):
        _identity(**{field: bad})


def test_source_identity_is_frozen_and_forbids_extras() -> None:
    identity = _identity()
    with pytest.raises(ValidationError):
        identity.actor_user_id = 5  # type: ignore[misc]
    with pytest.raises(ValidationError):
        SourcePurgeJobIdentity(**identity.model_dump(), extra=1)


def test_document_identity_and_limit_error_contracts() -> None:
    values = {"operation_id": OP, "workspace_id": WS, "actor_user_id": 11, "membership_revision": 3,
              "configuration_revision": 7, "source_id": SRC, "source_generation": 2, "document_id": uuid4()}
    DocumentCleanupJobIdentity(**values)
    with pytest.raises(ValidationError):
        DocumentCleanupJobIdentity(**{**values, "configuration_revision": 0})
    error = DocumentCleanupPreparationLimitError("timeline")
    assert str(error) == "timeline" and error.owner_code == "timeline"
    with pytest.raises(ValueError):
        DocumentCleanupPreparationLimitError("chat")


# --- start_source_purge ------------------------------------------------------------------------

async def _start(current):
    scope = WorkspaceContext(11, WS, "owner", 3)
    source = SimpleNamespace(id=SRC, status="active", generation=1, retired_at=None)
    session = _session()
    session.scalar = AsyncMock(return_value=current)
    with patch.object(public, "_lock_source_row", AsyncMock(return_value=(source, _fence(config=9)))), \
         patch.object(public, "_fence_connector_source", AsyncMock()), \
         patch.object(public, "commit_with_replay", AsyncMock()), \
         patch.object(public, "make_source_change", MagicMock(return_value=object())), \
         patch("modules.ingestion.public.publish_event", AsyncMock()):
        result = await public.start_source_purge(session, SRC, scope=scope, multi_workspace_enabled=True)
    return session, result


async def test_start_source_purge_captures_access_fence_configuration() -> None:
    session, _ = await _start(None)
    operation = session.add.call_args.args[0]
    assert operation.configuration_revision == 9
    assert operation.membership_revision == 3


async def test_start_source_purge_does_not_recapture_existing_operation() -> None:
    existing = SimpleNamespace(status="queued", configuration_revision=None)
    session, result = await _start(existing)
    assert result is existing and existing.configuration_revision is None
    session.add.assert_not_called()


# --- capture reader / identity / resolver ------------------------------------------------------

async def test_capture_reader_returns_dto_on_exact_match() -> None:
    with patch.object(public, "_admit_source_scope", AsyncMock(return_value=_fence())):
        capture = await public.read_source_purge_job_capture(
            _session(_row()), OP, scope=_scope(), multi_workspace_enabled=True)
    assert capture == _identity()


@pytest.mark.parametrize("row,fence", [
    (_row(config=None), _fence()),   # legacy NULL capture is quarantined
    (_row(config=7), _fence(config=8)),  # stale configuration
    (None, _fence()),
])
async def test_capture_reader_null_or_stale_returns_none(row, fence) -> None:
    with patch.object(public, "_admit_source_scope", AsyncMock(return_value=fence)):
        assert await public.read_source_purge_job_capture(
            _session(row), OP, scope=_scope(), multi_workspace_enabled=True) is None


async def test_capture_reader_query_binds_membership_and_selects_no_uri() -> None:
    session = _session(_row())
    with patch.object(public, "_admit_source_scope", AsyncMock(return_value=_fence())):
        await public.read_source_purge_job_capture(session, OP, scope=_scope(), multi_workspace_enabled=True)
    sql = _sql(session.execute.await_args.args[0])
    assert "membership_revision = 3" in sql and "raw_uris" not in sql and "FROM sources" not in sql.replace("source_purge", "")


async def test_identity_delegates_to_capture_and_projects_scope() -> None:
    with patch.object(public, "read_source_purge_job_capture", AsyncMock(return_value=_identity())) as read:
        assert await public.read_source_purge_job_identity(
            None, OP, scope=_scope(), multi_workspace_enabled=True) == _scope()
    read.assert_awaited_once()
    with patch.object(public, "read_source_purge_job_capture", AsyncMock(return_value=None)):
        assert await public.read_source_purge_job_identity(
            None, OP, scope=_scope(), multi_workspace_enabled=True) is None


async def test_resolver_null_configuration_never_authorizes() -> None:
    authorize = AsyncMock()
    with patch.object(public.workspaces, "authorize_internal_job", authorize):
        assert await public.resolve_source_purge_job_scope(
            _session(_row(config=None)), OP, multi_workspace_enabled=True) is None
    authorize.assert_not_awaited()


async def test_resolver_fence_mismatch_returns_none() -> None:
    with patch.object(public.workspaces, "authorize_internal_job", AsyncMock(return_value=_fence(config=8))):
        assert await public.resolve_source_purge_job_scope(
            _session(_row()), OP, multi_workspace_enabled=True) is None


async def test_resolver_permission_loss_propagates() -> None:
    with patch.object(public.workspaces, "authorize_internal_job", AsyncMock(side_effect=HTTPException(403))),          pytest.raises(HTTPException):
        await public.resolve_source_purge_job_scope(_session(_row()), OP, multi_workspace_enabled=True)


async def test_resolver_returns_exact_scope_after_capture_reread() -> None:
    with patch.object(public.workspaces, "authorize_internal_job", AsyncMock(return_value=_fence())), \
         patch.object(public, "read_source_purge_job_capture", AsyncMock(return_value=_identity())):
        assert await public.resolve_source_purge_job_scope(
            _session(_row()), OP, multi_workspace_enabled=True) == _scope()
    with patch.object(public.workspaces, "authorize_internal_job", AsyncMock(return_value=_fence())), \
         patch.object(public, "read_source_purge_job_capture", AsyncMock(return_value=_identity(source_generation=5))):
        assert await public.resolve_source_purge_job_scope(
            _session(_row()), OP, multi_workspace_enabled=True) is None


# --- observer discovery / coverage -------------------------------------------------------------

async def test_observer_discovery_is_identity_only_and_bounded() -> None:
    session = _session()
    await public.discover_source_purge_observer_ids(session, workspace_id=WS, source_id=SRC, limit=100)
    sql = _sql(session.scalars.await_args.args[0])
    assert f"workspace_id = '{WS}'" in sql and f"source_id = '{SRC}'" in sql
    assert "membership_revision" not in sql and "generation" not in sql and "actor_user_id" not in sql
    assert "LIMIT 100" in sql and "ORDER BY source_purge_operations.id" in sql
    for bad in (0, 101):
        with pytest.raises(ValueError):
            await public.discover_source_purge_observer_ids(session, workspace_id=WS, source_id=SRC, limit=bad)


async def test_pending_coverage_keeps_failed_memory_done_operation_with_unsettled_children() -> None:
    session = _session()
    with patch.object(public, "_admit_source_scope", AsyncMock(return_value=_fence())):
        await public.pending_source_coverage_ids(session, scope=_scope(), multi_workspace_enabled=True)
    sql, params = _bound(session.scalars.await_args.args[0])
    # exclusion is narrowed: settled only when no pending child count and no pending owner codes
    assert "pending_child_count IS NOT NULL" in sql and "pending_child_count = %" in sql
    assert "pending_owner_codes = CAST(" in sql and [] in params
    assert "memory_error_code IN" in sql


def test_global_reconcile_exclusion_is_shared_with_scoped_page() -> None:
    sql, _ = _bound(public.coverage_settled_exclusion())
    assert "memory_cache_pending IS false" in sql and "pending_owner_codes" in sql


# --- worker admission --------------------------------------------------------------------------

def _delivery(status: str = "queued"):
    return SimpleNamespace(status=status, dispatched_at=datetime.now(UTC), type="source.purge.requested",
                           version=1, producer="modules.sources", workspace_id=WS, actor_user_id=11,
                           membership_revision=3, payload=worker._purge_payload(OP, _scope()))


async def _admit(capture, fence):
    with patch.object(worker.ingestion, "resolve_ingestion_event_scope", AsyncMock(return_value=_scope())), \
         patch.object(worker.ingestion, "get_event_delivery", AsyncMock(return_value=_delivery())), \
         patch.object(worker.sources, "read_source_purge_job_capture", AsyncMock(return_value=capture)), \
         patch.object(worker, "read_access_fence", AsyncMock(return_value=fence)):
        return await worker._admit_event(None, uuid4(), multi_workspace_enabled=True, event_types=worker._PURGE_EVENTS)


async def test_admit_event_returns_tuple_when_fence_equals_capture() -> None:
    admitted = await _admit(_identity(), _fence())
    assert admitted is not None and admitted[0] == OP and admitted[2] == _fence()


async def test_admit_event_config_mismatch_is_noop() -> None:
    assert await _admit(_identity(), _fence(config=8)) is None
    assert await _admit(None, _fence()) is None


async def test_admit_event_snapshot_fence_drift_is_noop() -> None:
    with patch.object(worker.ingestion, "resolve_ingestion_event_scope", AsyncMock(return_value=_scope())), \
         patch.object(worker.ingestion, "get_event_delivery", AsyncMock(return_value=_delivery())), \
         patch.object(worker.sources, "read_source_purge_job_capture", AsyncMock(return_value=_identity())), \
         patch.object(worker, "read_access_fence", AsyncMock(return_value=_fence())):
        assert await worker._admit_event(
            None, uuid4(), multi_workspace_enabled=True, event_types=worker._PURGE_EVENTS,
            expected_fence=_fence(membership=4)) is None


# --- process_source_purge dependency limit -----------------------------------------------------

async def test_process_source_purge_dependency_limit_fails_operation_and_delivers() -> None:
    operation = SimpleNamespace(id=OP, generation=2, documents_status="queued", status="queued",
                                error_code=None, pending_owner_codes=[])
    source = SimpleNamespace(id=SRC, workspace_id=WS, status="archived", generation=2, local_only=False)
    delivery = _delivery()
    session = MagicMock()
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    ctx = {"session_factory": factory, "settings": SimpleNamespace(multi_workspace_enabled=True)}
    set_delivery, commit = AsyncMock(), AsyncMock()
    with patch.object(worker, "_admit_event", AsyncMock(return_value=(OP, _scope(), _fence(), delivery))), \
         patch.object(worker, "_lock_operation", AsyncMock(return_value=(source, operation))), \
         patch.object(worker, "_lock_delivery", AsyncMock(return_value=delivery)), \
         patch.object(worker.documents, "delete_source_documents_in_uow", AsyncMock(
             side_effect=DocumentCleanupPreparationLimitError("relationships")), create=True), \
         patch.object(worker.ingestion, "set_event_delivery", set_delivery), \
         patch.object(worker, "commit_with_replay", commit), \
         patch.object(worker, "make_source_change", MagicMock(return_value=object())):
        await worker.process_source_purge(ctx, str(uuid4()))
    assert (operation.status, operation.documents_status) == ("failed", "failed")
    assert operation.error_code == "source_cleanup_dependency_limit_exceeded"
    assert operation.pending_owner_codes == ["documents"]
    assert set_delivery.await_args.args[2] == "delivered"
    commit.assert_awaited_once()
    assert commit.await_args.kwargs["access_fence"] == _fence()


async def test_process_source_purge_other_errors_propagate() -> None:
    operation = SimpleNamespace(id=OP, generation=2, documents_status="queued", status="queued",
                                error_code=None, pending_owner_codes=[])
    source = SimpleNamespace(id=SRC, workspace_id=WS, status="archived", generation=2, local_only=False)
    delivery = _delivery()
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    ctx = {"session_factory": factory, "settings": SimpleNamespace(multi_workspace_enabled=True)}
    with patch.object(worker, "_admit_event", AsyncMock(return_value=(OP, _scope(), _fence(), delivery))), \
         patch.object(worker, "_lock_operation", AsyncMock(return_value=(source, operation))), \
         patch.object(worker, "_lock_delivery", AsyncMock(return_value=delivery)), \
         patch.object(worker.documents, "delete_source_documents_in_uow", AsyncMock(
             side_effect=RuntimeError("boom")), create=True), \
         patch.object(worker, "make_source_change", MagicMock(return_value=object())),          pytest.raises(RuntimeError):
        await worker.process_source_purge(ctx, str(uuid4()))
    assert isinstance(UUID(str(OP)), UUID)


# --- snapshot fence carry (review P3-2) --------------------------------------------------------

async def _drift_run(fn, status="queued"):
    lock = AsyncMock(return_value=(None, None))
    session = MagicMock()
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    delivery = _delivery()
    delivery.type = "source.purge.coverage"
    delivery.status = status
    with patch.object(worker.ingestion, "resolve_ingestion_event_scope", AsyncMock(return_value=_scope())), \
         patch.object(worker.ingestion, "get_event_delivery", AsyncMock(return_value=delivery)), \
         patch.object(worker.sources, "read_source_purge_job_capture", AsyncMock(return_value=_identity())), \
         patch.object(worker, "read_access_fence", AsyncMock(return_value=_fence(membership=4))), \
         patch.object(worker, "_lock_operation", lock):
        await fn(factory, delivery)
    return lock


async def test_recover_memory_coverage_fence_drift_is_noop() -> None:
    lock = await _drift_run(
        lambda factory, d: worker._recover_memory_coverage(
            factory, uuid4(), (), reset=False, scope=_scope(), multi_workspace_enabled=True,
            dispatched_at=d.dispatched_at, access_fence=_fence()))
    lock.assert_not_awaited()


async def test_evict_memory_cache_fence_drift_is_noop() -> None:
    lock = await _drift_run(
        lambda factory, d: worker._evict_memory_cache_after_commit(
            factory, MagicMock(), OP, uuid4(), scope=_scope(), multi_workspace_enabled=True,
            dispatched_at=d.dispatched_at, attempt=(), access_fence=_fence()), status="pending")
    lock.assert_not_awaited()


async def test_memory_coverage_passes_admitted_fence_to_recovery() -> None:
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    ctx = {"session_factory": factory, "redis": MagicMock(), "settings": SimpleNamespace(multi_workspace_enabled=True)}
    recover = AsyncMock()
    with patch.object(worker, "_admit_event", AsyncMock(return_value=(OP, _scope(), _fence(), _delivery()))), \
         patch.object(worker, "_lock_operation", AsyncMock(side_effect=RuntimeError("boom"))), \
         patch.object(worker, "_recover_memory_coverage", recover):
        await worker.process_source_memory_coverage(ctx, str(uuid4()))
    recover.assert_awaited_once()
    assert recover.await_args.kwargs["access_fence"] == _fence()
