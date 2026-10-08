"""Cleanup delivery admission: scope resolve branch, dispatcher envelope and claim helpers (D2b-2)."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4, uuid5

import pytest
from sqlalchemy.dialects import postgresql

from core.workspaces.schemas import InternalJobScope
from modules.ingestion import dispatcher
from modules.ingestion import public as ingestion
from modules.knowledge.documents.schemas import DocumentCleanupJobIdentity

WS, SRC, OP = uuid4(), uuid4(), uuid4()
EVENT_ID = uuid5(OP, "document-cleanup-requested")
JOB_SCOPE = InternalJobScope(
    workspace_id=WS, actor_user_id=7, membership_revision=3, source_id=SRC, source_generation=5,
)


def _identity(**overrides) -> DocumentCleanupJobIdentity:
    values = {"operation_id": OP, "workspace_id": WS, "actor_user_id": 7, "membership_revision": 3,
              "configuration_revision": 2, "source_id": SRC, "source_generation": 5, "document_id": uuid4()}
    return DocumentCleanupJobIdentity(**{**values, **overrides})


def _row(**overrides):
    values = {"workspace_id": WS, "actor_user_id": 7, "membership_revision": 3, "version": 1,
              "producer": "modules.knowledge.documents", "payload_operation": str(OP), "operation_type": "string"}
    return SimpleNamespace(**{**values, **overrides})


def _sql(statement) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


async def _resolve(row, *, exact=EVENT_ID, identity=None, event_id=EVENT_ID):
    session = MagicMock()
    session.scalar = AsyncMock(return_value=exact)
    resolver = AsyncMock(return_value=identity)
    with patch.object(ingestion.documents, "resolve_document_cleanup_job_identity", resolver):
        result = await ingestion._resolve_document_cleanup_event_scope(
            session, event_id, row, multi_workspace_enabled=True,
        )
    return result, session, resolver


async def test_resolve_branch_returns_source_bound_scope() -> None:
    scope, session, resolver = await _resolve(_row(), identity=_identity())
    assert scope == JOB_SCOPE
    assert "payload" in _sql(session.scalar.await_args.args[0])  # exact JSONB equality
    resolver.assert_awaited_once()


async def test_resolve_branch_extra_payload_key_is_none_without_authorization() -> None:
    scope, _, resolver = await _resolve(_row(), exact=None, identity=_identity())
    assert scope is None
    resolver.assert_not_awaited()


@pytest.mark.parametrize("changed", [None, {"actor_user_id": 8}, {"membership_revision": 4}, {"workspace_id": uuid4()}])
async def test_resolve_branch_principal_mismatch_is_none(changed) -> None:
    scope, _, _ = await _resolve(_row(), identity=_identity(**changed) if changed else None)
    assert scope is None


@pytest.mark.parametrize("row", [
    _row(operation_type="object"), _row(version=2), _row(producer="modules.sources"),
    _row(payload_operation=str(OP).upper()), _row(payload_operation="x"),
])
async def test_resolve_branch_rejects_noncanonical_rows(row) -> None:
    scope, _, resolver = await _resolve(row, identity=_identity())
    assert scope is None
    resolver.assert_not_awaited()


async def test_resolve_branch_rejects_nondeterministic_event_id() -> None:
    scope, _, _ = await _resolve(_row(), identity=_identity(), event_id=uuid4())
    assert scope is None


def _outbox(**overrides):
    values = {"id": EVENT_ID, "type": "document.cleanup.requested", "version": 1,
              "producer": "modules.knowledge.documents", "payload": {"operation_id": str(OP)},
              "workspace_id": WS, "actor_user_id": 7, "membership_revision": 3}
    return SimpleNamespace(**{**values, **overrides})


def test_valid_event_envelope_accepts_exact_operation_only_cleanup() -> None:
    assert dispatcher.valid_event_envelope(_outbox(), JOB_SCOPE) is True


@pytest.mark.parametrize("override", [
    {"payload": {"operation_id": str(OP), "workspace_id": str(WS), "actor_user_id": 7,
                 "membership_revision": 3, "source_id": str(SRC), "source_generation": 5}},  # old shape
    {"payload": {}}, {"payload": {"operation_id": str(OP).upper()}}, {"payload": {"operation_id": 5}},
    {"id": uuid4()}, {"actor_user_id": 8}, {"workspace_id": uuid4()}, {"membership_revision": 9},
    {"producer": "modules.sources"}, {"version": 2},
])
def test_valid_event_envelope_rejects_other_cleanup_shapes(override) -> None:
    assert dispatcher.valid_event_envelope(_outbox(**override), JOB_SCOPE) is False


def test_other_event_types_keep_payload_principal_requirement() -> None:
    event = _outbox(type="source.purge.requested", producer="modules.sources", payload={"operation_id": str(OP)})
    assert dispatcher.valid_event_envelope(event, JOB_SCOPE) is False


async def test_settle_cas_sql_binds_claim_status_and_exact_payload() -> None:
    session = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = EVENT_ID
    session.execute = AsyncMock(return_value=result)
    claim = datetime(2026, 1, 1, tzinfo=UTC)
    with patch.object(ingestion, "_admit_ingestion_scope", AsyncMock()):
        ok = await ingestion.settle_document_cleanup_event_in_uow(
            session, EVENT_ID, "pending", operation_id=OP, dispatched_at=claim,
            next_attempt_at=claim, scope=JOB_SCOPE, multi_workspace_enabled=True,
        )
    assert ok is True
    sql = _sql(session.execute.await_args.args[0])
    assert "status =" in sql and "dispatched_at IS NOT DISTINCT FROM" in sql
    assert "payload" in sql and "workspace_id" in sql and "actor_user_id" in sql


async def test_get_and_lock_helpers_use_principal_columns_not_source_payload() -> None:
    session = MagicMock()
    session.scalar = AsyncMock(return_value=None)
    with patch.object(ingestion, "_admit_ingestion_scope", AsyncMock()):
        assert await ingestion.get_document_cleanup_event(
            session, EVENT_ID, operation_id=OP, scope=JOB_SCOPE, multi_workspace_enabled=True) is None
        assert await ingestion.lock_document_cleanup_event_in_uow(
            session, EVENT_ID, operation_id=OP, scope=JOB_SCOPE, multi_workspace_enabled=True) is None
    plain, locked = (_sql(call.args[0]) for call in session.scalar.await_args_list)
    assert "FOR UPDATE" not in plain and "FOR UPDATE" in locked
    for sql in (plain, locked):
        assert "source_generation" not in sql and "workspace_id" in sql
