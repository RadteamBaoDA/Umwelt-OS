"""Documents retained cleanup authority (D2a-3): identity readers, closure, deletes, wakeups, status.

Mocked sessions only (no DB, no Docker); assertions use compiled SQL and recorded call order.
"""

import dataclasses
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4, uuid5

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError

from core.workspaces.schemas import AccessFence, InternalJobScope
from modules.ingestion import public as ingestion
from modules.knowledge.documents import public, routes
from modules.knowledge.documents.models import DocumentCleanupOperation
from modules.knowledge.documents.schemas import (
    DocumentCleanupJobIdentity,
    DocumentCleanupPreparationLimitError,
)
from modules.knowledge.observations import public as observations
from modules.sources.schemas import SourceFence, SourcePurgeJobIdentity
from tests.unit.modules.knowledge.documents._scope import FENCE, SCOPE, SCOPE_KW, WORKSPACE_ID

SRC, DOC, OP = uuid4(), uuid4(), uuid4()
SOURCE_FENCE = SourceFence(id=SRC, workspace_id=WORKSPACE_ID, status="active", generation=4, local_only=False)


def _sql(statement) -> str:
    return str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


def _row(**overrides):
    values = {"id": OP, "workspace_id": WORKSPACE_ID, "actor_user_id": 1, "membership_revision": 1,
              "configuration_revision": 1, "source_id": SRC, "source_generation": 4, "document_id": DOC}
    return SimpleNamespace(**{**values, **overrides})


def _session(*rows):
    session = MagicMock()
    results = []
    for row in rows:
        result = MagicMock()
        result.one_or_none.return_value = row
        results.append(result)
    session.execute = AsyncMock(side_effect=results)
    return session


def _identity() -> DocumentCleanupJobIdentity:
    return DocumentCleanupJobIdentity(
        operation_id=OP, workspace_id=WORKSPACE_ID, actor_user_id=1, membership_revision=1,
        configuration_revision=1, source_id=SRC, source_generation=4, document_id=DOC)


# --- contracts ------------------------------------------------------------------------------------

@pytest.mark.parametrize("field", ["actor_user_id", "membership_revision", "configuration_revision",
                                   "source_generation"])
@pytest.mark.parametrize("bad", [None, 0, -1, True])
def test_identity_rejects_partial_zero_and_bool_epochs(field, bad) -> None:
    values = _identity().model_dump()
    values[field] = bad
    with pytest.raises(ValidationError):
        DocumentCleanupJobIdentity(**values)


def test_limit_error_owner_allowlist() -> None:
    assert str(DocumentCleanupPreparationLimitError("timeline")) == "timeline"
    with pytest.raises(ValueError):
        DocumentCleanupPreparationLimitError("chat")


# --- resolver / reader ------------------------------------------------------------------------------

@pytest.mark.parametrize("column", ["membership_revision", "configuration_revision", "source_generation"])
async def test_resolver_null_epoch_never_authorizes(column) -> None:
    authorize = AsyncMock()
    with patch.object(public, "authorize_internal_job", authorize):
        assert await public.resolve_document_cleanup_job_identity(
            _session(_row(**{column: None})), OP, multi_workspace_enabled=False) is None
    authorize.assert_not_awaited()


async def test_resolver_missing_receipt_is_none() -> None:
    assert await public.resolve_document_cleanup_job_identity(
        _session(None), OP, multi_workspace_enabled=False) is None


async def test_resolver_stale_configuration_is_none() -> None:
    authorize = AsyncMock(return_value=AccessFence(WORKSPACE_ID, 1, 1, 2))
    with patch.object(public, "authorize_internal_job", authorize):
        assert await public.resolve_document_cleanup_job_identity(
            _session(_row()), OP, multi_workspace_enabled=False) is None
    authorize.assert_awaited_once()


async def test_resolver_returns_exact_identity_after_workspace_scoped_reread() -> None:
    session = _session(_row(), _row())
    with patch.object(public, "authorize_internal_job", AsyncMock(return_value=FENCE)):
        identity = await public.resolve_document_cleanup_job_identity(session, OP, multi_workspace_enabled=False)
    assert identity == _identity()
    assert "workspace_id" in _sql(session.execute.await_args_list[1].args[0])
    assert "membership_revision" in _sql(session.execute.await_args_list[1].args[0])


async def test_resolver_permission_loss_propagates() -> None:
    with patch.object(public, "authorize_internal_job", AsyncMock(side_effect=HTTPException(403))), \
            pytest.raises(HTTPException):
        await public.resolve_document_cleanup_job_identity(_session(_row()), OP, multi_workspace_enabled=False)


async def test_reader_mismatched_membership_or_configuration_is_none() -> None:
    session = _session(None)
    assert await public.read_document_cleanup_job_identity(session, OP, **SCOPE_KW) is None
    sql = _sql(session.execute.await_args.args[0])
    assert "membership_revision = 1" in sql and "actor_user_id = 1" in sql and str(WORKSPACE_ID) in sql
    assert await public.read_document_cleanup_job_identity(
        _session(_row(configuration_revision=2)), OP, **SCOPE_KW) is None


async def test_reader_source_bound_scope_requires_matching_source_and_generation() -> None:
    bound = InternalJobScope(WORKSPACE_ID, 1, 1, SRC, 4)
    assert await public.read_document_cleanup_job_identity(
        _session(_row()), OP, scope=bound, multi_workspace_enabled=False) == _identity()
    other = InternalJobScope(WORKSPACE_ID, 1, 1, SRC, 5)
    assert await public.read_document_cleanup_job_identity(
        _session(_row()), OP, scope=other, multi_workspace_enabled=False) is None


async def test_reader_member_is_denied_before_sql() -> None:
    session = _session()
    with pytest.raises(HTTPException) as exc:
        await public.read_document_cleanup_job_identity(
            session, OP, scope=dataclasses.replace(SCOPE, role="member"), multi_workspace_enabled=False)
    assert exc.value.status_code == 403
    session.execute.assert_not_awaited()


# --- closure coordinator ---------------------------------------------------------------------------------

def _empty_owner(**extra):
    base = {"overflow": False, "membership_ids": (), "entity_ids": (), "endpoint_entity_ids": (),
            "participant_entity_ids": ()}
    return SimpleNamespace(**{**base, **extra})


def _closure_session(count: int = 0):
    session = MagicMock()
    session.scalars = AsyncMock(return_value=MagicMock(all=lambda: [uuid4() for _ in range(count)]))
    session.execute = AsyncMock(return_value=MagicMock(all=list))
    return session


def _owner_patches(overflow: str | None = None):
    from modules.knowledge.entities import public as entities
    from modules.knowledge.relationships import public as relationships
    from modules.knowledge.temporal import public as temporal
    from modules.timeline import public as timeline

    def owner(name):
        return AsyncMock(return_value=_empty_owner(overflow=overflow == name))
    return (
        patch.object(public, "list_evidence_ref_keys", AsyncMock(return_value=([], False))),
        patch.object(observations, "observation_cleanup_ids", owner("observations")),
        patch.object(entities, "support_cleanup_ids", owner("entities")),
        patch.object(relationships, "support_cleanup_ids", owner("relationships")),
        patch.object(timeline, "support_cleanup_ids", owner("timeline")),
        patch.object(temporal, "tombstone_cleanup_ids", owner("temporal")),
    )


async def _closure(session, document_id=None, overflow=None):
    patches = _owner_patches(overflow)
    for item in patches:
        item.start()
    try:
        return await public._prepare_cleanup_closure(
            session, source_id=SRC, document_id=document_id, scope=SCOPE, flag=False,
            access_fence=FENCE, source_fence=SOURCE_FENCE)
    finally:
        for item in patches:
            item.stop()


@pytest.mark.parametrize("owner", ["observations", "entities", "relationships", "timeline", "temporal"])
async def test_closure_reports_first_overflowed_owner_without_raising(owner) -> None:
    result = await _closure(_closure_session(), overflow=owner)
    assert isinstance(result, DocumentCleanupPreparationLimitError) and result.owner_code == owner


async def test_closure_reports_documents_overflow_and_stops_before_owners() -> None:
    result = await _closure(_closure_session(10_001))
    assert isinstance(result, DocumentCleanupPreparationLimitError) and result.owner_code == "documents"


async def test_closure_discovery_is_workspace_qualified_and_nonlocking() -> None:
    session = _closure_session()
    result = await _closure(session)
    assert not isinstance(result, DocumentCleanupPreparationLimitError)
    for call in session.scalars.await_args_list:
        sql = _sql(call.args[0])
        assert "FOR UPDATE" not in sql
    assert str(WORKSPACE_ID) in _sql(session.scalars.await_args_list[0].args[0])


async def test_closure_rejects_changed_fences() -> None:
    patches = _owner_patches()
    for item in patches:
        item.start()
    try:
        with pytest.raises(HTTPException) as exc:
            await public._prepare_cleanup_closure(
                _closure_session(), source_id=SRC, document_id=None, scope=SCOPE, flag=False,
                access_fence=dataclasses.replace(FENCE, configuration_revision=9), source_fence=SOURCE_FENCE)
    finally:
        for item in patches:
            item.stop()
    assert exc.value.status_code == 409


# --- lock phase ordering --------------------------------------------------------------------------------

async def test_lock_phase_order_documents_children_uri_identity_then_owners() -> None:
    from modules.knowledge.entities import public as entities
    from modules.knowledge.relationships import public as relationships
    from modules.knowledge.temporal import public as temporal
    from modules.timeline import public as timeline

    calls: list[str] = []
    session = MagicMock()

    async def scalars(statement):
        sql = _sql(statement)
        assert "FOR UPDATE" in sql
        calls.append(sql.split("FROM ")[1].split()[0])
        return MagicMock()

    async def execute(statement, *args):
        calls.append("advisory" if "pg_advisory_xact_lock" in str(statement) else "other")
        assert "workspace_id" in str(statement)
        return MagicMock()

    session.scalars, session.execute = scalars, execute

    def recorder(name):
        return AsyncMock(side_effect=lambda *a, **k: calls.append(name))

    ids = (uuid4(),)
    closure = SimpleNamespace(
        source_id=SRC, document_id=None, document_ids=ids, version_ids=ids, chunk_ids=ids, provenance_ids=ids,
        identity_ids=ids, observations=object(), entities=object(), relationships=object(),
        timeline=object(), temporal=object(), entity_union=(uuid4(),))
    with patch.object(observations, "prepare_document_cleanup_in_uow", recorder("O")), \
            patch.object(entities, "prepare_support_cleanup_in_uow", recorder("E")), \
            patch.object(relationships, "prepare_support_cleanup_in_uow", recorder("R")), \
            patch.object(timeline, "prepare_support_cleanup_in_uow", recorder("T")), \
            patch.object(temporal, "prepare_tombstone_scope_in_uow", recorder("G")):
        await public._lock_cleanup_closure(
            session, closure, scope=SCOPE, flag=False, access_fence=FENCE, source_fence=SOURCE_FENCE)
    assert calls == ["documents", "document_versions", "document_chunks", "normalized_version_provenance",
                     "advisory", "normalized_document_identities", "O", "E", "R", "T", "G"]


# --- delete_document ----------------------------------------------------------------------------------

def _delete_harness(closure):
    calls: list[str] = []
    session = MagicMock()
    document = SimpleNamespace(id=DOC, raw_uri="file://x", external_id="ext", source_id=SRC)
    session.scalar = AsyncMock(side_effect=lambda statement: calls.append("document") or (
        document if "FOR UPDATE" in _sql(statement) and "documents" in _sql(statement) else
        SimpleNamespace(tombstoned_at=None, document_id=DOC)))
    deleted = MagicMock()
    deleted.first.return_value = DOC
    session.scalars = AsyncMock(return_value=deleted)
    added: list[object] = []
    session.add = MagicMock(side_effect=lambda obj: added.append(obj))
    session.flush = AsyncMock(side_effect=lambda: [setattr(o, "id", o.id or uuid4()) for o in added if isinstance(o, DocumentCleanupOperation)])
    session.refresh = AsyncMock()

    def rec(name, result=None):
        return AsyncMock(side_effect=lambda *a, **k: calls.append(name) or result)

    published = AsyncMock(side_effect=lambda *a, **k: calls.append("publish"))
    commit = AsyncMock(side_effect=lambda *a, **k: calls.append("commit"))
    patches = [
        patch.object(public, "_read_document_source_id", AsyncMock(return_value=SRC)),
        patch.object(public.sources, "lock_source_set", AsyncMock(
            side_effect=lambda *a, **k: calls.append("lock_source_set") or SimpleNamespace(
                fences=(SOURCE_FENCE,), access_fence=FENCE))),
        patch.object(public, "_prepare_cleanup_closure", AsyncMock(return_value=closure)),
        patch.object(public, "_lock_cleanup_closure", rec("lock_closure")),
        patch.object(public, "capture_document_cleanup_evidence", rec("evidence")),
        patch.object(public, "_apply_graph_cleanup", rec("graph", [])),
        patch.object(public, "commit_with_replay", commit),
        patch.object(public, "make_knowledge_change", MagicMock(return_value="change")),
        patch.object(ingestion, "prepare_document_materializations_in_uow", rec("I_prepare")),
        patch.object(ingestion, "publish_event", published),
        patch.object(ingestion, "tombstone_document_materializations", rec("I_tombstone")),
        patch.object(observations, "purge_document_in_uow", rec("O_purge")),
    ]
    return session, calls, added, published, commit, patches


async def test_delete_document_order_epochs_payload_and_commit_fence() -> None:
    closure = SimpleNamespace(observations="obs-closure")
    session, calls, added, published, commit, patches = _delete_harness(closure)
    for item in patches:
        item.start()
    try:
        operation = await public.delete_document(session, DOC, **SCOPE_KW)
    finally:
        for item in patches:
            item.stop()
    order = [c for c in calls if c != "document"]
    assert order == ["lock_source_set", "lock_closure", "I_prepare", "evidence", "publish", "O_purge",
                     "I_tombstone", "graph", "commit"]
    receipt = added[0]
    assert isinstance(receipt, DocumentCleanupOperation) and operation is receipt
    assert (receipt.workspace_id, receipt.actor_user_id, receipt.membership_revision,
            receipt.configuration_revision, receipt.source_generation) == (WORKSPACE_ID, 1, 1, 1, 4)
    event = published.await_args.args[1]
    assert event.payload == {"operation_id": str(receipt.id)}
    assert event.id == uuid5(receipt.id, "document-cleanup-requested")
    assert published.await_args.kwargs["scope"] == SCOPE
    assert commit.await_args.kwargs["access_fence"] == FENCE and commit.await_args.kwargs["scope"] == SCOPE


async def test_delete_document_owner_overflow_raises_before_any_effect() -> None:
    session, calls, added, published, commit, patches = _delete_harness(
        DocumentCleanupPreparationLimitError("relationships"))
    for item in patches:
        item.start()
    try:
        with pytest.raises(DocumentCleanupPreparationLimitError):
            await public.delete_document(session, DOC, **SCOPE_KW)
    finally:
        for item in patches:
            item.stop()
    assert not added and "lock_closure" not in calls and "I_prepare" not in calls
    published.assert_not_awaited()
    commit.assert_not_awaited()
    session.execute.assert_not_called()
    session.scalars.assert_not_awaited()


async def test_delete_document_foreign_document_is_none_and_member_denied() -> None:
    with patch.object(public, "_read_document_source_id", AsyncMock(return_value=None)):
        assert await public.delete_document(MagicMock(), DOC, **SCOPE_KW) is None
    session = MagicMock()
    session.scalar = AsyncMock()
    with pytest.raises(HTTPException) as exc:
        await public.delete_document(
            session, DOC, scope=dataclasses.replace(SCOPE, role="member"), multi_workspace_enabled=False)
    assert exc.value.status_code == 403
    session.scalar.assert_not_awaited()


# --- routes --------------------------------------------------------------------------------------------

REQUEST = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
    settings=SimpleNamespace(multi_workspace_enabled=False))))


async def test_route_delete_maps_dependency_limit_to_409() -> None:
    with patch.object(routes, "_lock_document_write_request", AsyncMock()), \
            patch.object(routes.public, "delete_document", AsyncMock(
                side_effect=DocumentCleanupPreparationLimitError("timeline"))), \
            pytest.raises(HTTPException) as exc:
        await routes.delete_document(DOC, object(), REQUEST, SCOPE)
    assert (exc.value.status_code, exc.value.detail) == (409, "document_cleanup_dependency_limit_exceeded")


async def test_route_delete_foreign_document_is_404() -> None:
    with patch.object(routes, "_lock_document_write_request", AsyncMock()), \
            patch.object(routes.public, "delete_document", AsyncMock(return_value=None)), \
            pytest.raises(HTTPException) as exc:
        await routes.delete_document(DOC, object(), REQUEST, SCOPE)
    assert exc.value.status_code == 404


async def test_route_status_member_denied_before_any_sql() -> None:
    reader = AsyncMock()
    with patch.object(routes.public, "get_document_cleanup_operation", reader), pytest.raises(HTTPException) as exc:
        await routes.get_deletion_operation(OP, object(), REQUEST, dataclasses.replace(SCOPE, role="member"))
    assert exc.value.status_code == 403
    reader.assert_not_awaited()


def _operation(**overrides):
    values = {
        "id": OP, "status": "queued", "record_status": "deleted", "graph_status": "tombstoned",
        "raw_status": "queued", "evidence_scope_status": "captured", "copied_status": "queued",
        "chat_status": "queued", "chat_error_code": None, "memory_status": "queued", "memory_error_code": None,
        "memory_unresolved_count": 0, "memory_cache_pending": False, "agent_status": "queued",
        "agent_error_code": None, "agent_unresolved_count": 0, "agent_waiting_for_lease": False,
        "materialization_status": "queued", "materialization_error_code": None,
        "materialization_unresolved_count": 0, "brief_status": "queued", "brief_error_code": None,
        "brief_unresolved_count": 0, "error_code": None, "copied_error_code": None,
        "membership_revision": 1, "configuration_revision": 1,
    }
    return SimpleNamespace(**{**values, **overrides})


@pytest.mark.parametrize(("overrides", "expected"), [
    ({"membership_revision": None, "configuration_revision": None}, "cleanup_authority_unavailable"),
    ({"configuration_revision": 2}, "cleanup_authority_stale"),
    ({"membership_revision": 2}, "cleanup_authority_stale"),
    ({}, None),
    ({"status": "succeeded", "configuration_revision": 2}, None),
])
async def test_route_status_projects_action_required(overrides, expected) -> None:
    operation = _operation(**overrides)
    with patch.object(routes.public, "get_document_cleanup_operation", AsyncMock(return_value=operation)):
        read = await routes.get_deletion_operation(OP, MagicMock(), REQUEST, SCOPE)
    assert read.error_code == expected


async def test_get_document_cleanup_operation_is_workspace_and_actor_scoped() -> None:
    session = MagicMock()
    session.scalar = AsyncMock(return_value=None)
    assert await public.get_document_cleanup_operation(session, OP, **SCOPE_KW) is None
    sql = _sql(session.scalar.await_args.args[0])
    assert str(WORKSPACE_ID) in sql and "actor_user_id = 1" in sql


# --- Source-wide lock/delete ---------------------------------------------------------------------------

async def test_lock_source_documents_overflow_returns_none_without_locking() -> None:
    session = _closure_session(10_001)
    with patch.object(public, "_lock_cleanup_closure", AsyncMock()) as lock:
        result = await public.lock_source_documents_for_purge_in_uow(
            session, SRC, access_fence=FENCE, source_fence=SOURCE_FENCE, **SCOPE_KW)
    assert result is None
    lock.assert_not_awaited()
    assert all("FOR UPDATE" not in _sql(c.args[0]) for c in session.scalars.await_args_list)


async def test_lock_source_documents_owner_overflow_returns_none() -> None:
    with patch.object(public, "_prepare_cleanup_closure",
                      AsyncMock(return_value=DocumentCleanupPreparationLimitError("entities"))), \
            patch.object(public, "_lock_cleanup_closure", AsyncMock()) as lock:
        assert await public.lock_source_documents_for_purge_in_uow(
            MagicMock(), SRC, access_fence=FENCE, source_fence=SOURCE_FENCE, **SCOPE_KW) is None
    lock.assert_not_awaited()


def _capture(**overrides) -> SourcePurgeJobIdentity:
    values = {"operation_id": OP, "workspace_id": WORKSPACE_ID, "actor_user_id": 1, "membership_revision": 1,
              "configuration_revision": 1, "source_id": SRC, "source_generation": 4}
    return SourcePurgeJobIdentity(**{**values, **overrides})


def _late_session(doc_count: int = 3):
    session = MagicMock()
    session.scalars = AsyncMock(return_value=MagicMock(all=lambda: [uuid4()] * doc_count))
    session.execute = AsyncMock()
    return session


async def _late(session, capture, closure):
    with patch.object(public.sources, "read_source_purge_job_capture", AsyncMock(return_value=capture)), \
            patch.object(public, "_prepare_cleanup_closure", AsyncMock(return_value=closure)):
        return await public.delete_source_documents_in_uow(
            session, SRC, source_purge_operation_id=OP, access_fence=FENCE, source_fence=SOURCE_FENCE,
            **SCOPE_KW)


async def test_late_delete_over_ten_thousand_documents_raises_exact_text_without_insert() -> None:
    session = _late_session(10_001)
    with pytest.raises(ValueError, match="^Source graph cleanup exceeds its atomic document limit$"):
        await _late(session, _capture(), object())
    session.execute.assert_not_awaited()


async def test_late_delete_dependency_overflow_raises_limit_before_insert() -> None:
    session = _late_session()
    with pytest.raises(DocumentCleanupPreparationLimitError):
        await _late(session, _capture(), DocumentCleanupPreparationLimitError("timeline"))
    session.execute.assert_not_awaited()


@pytest.mark.parametrize("capture", [None, "config", "membership", "generation"])
async def test_late_delete_capture_differing_from_fences_is_runtime_error(capture) -> None:
    value = {"config": _capture(configuration_revision=2), "membership": _capture(membership_revision=2),
             "generation": _capture(source_generation=9), None: None}[capture]
    session = _late_session()
    with pytest.raises(RuntimeError):
        await _late(session, value, object())
    session.execute.assert_not_awaited()


async def test_late_delete_inserts_identity_columns_takes_no_locks_and_publishes_scoped_events() -> None:
    session = _late_session()
    statements: list[str] = []

    async def execute(statement):
        statements.append(_sql(statement))
        return MagicMock()

    session.execute = execute
    receipt_ids = [uuid4(), uuid4()]
    pages = [receipt_ids, []]
    scalar_results = [MagicMock(all=lambda: [uuid4()]), MagicMock(all=lambda: pages.pop(0))]
    session.scalars = AsyncMock(side_effect=lambda *a, **k: scalar_results.pop(0) if scalar_results else MagicMock(all=list))
    closure = SimpleNamespace(observations="obs")
    publish = AsyncMock()
    with patch.object(ingestion, "publish_event", publish), \
            patch.object(observations, "purge_source_in_uow", AsyncMock()), \
            patch.object(public, "_apply_graph_cleanup", AsyncMock(return_value=["draft"])):
        drafts = await _late(session, _capture(), closure)
    assert drafts == ["draft"]
    insert = next(s for s in statements if "INSERT INTO document_cleanup_operations" in s)
    for column in ("workspace_id", "actor_user_id", "membership_revision", "configuration_revision",
                   "source_generation"):
        assert column in insert
    assert str(WORKSPACE_ID) in insert and "documents.workspace_id" in insert
    evidence = [s for s in statements if "INSERT INTO document_cleanup_evidence_references" in s]
    assert len(evidence) == 2 and all("workspace_id" in s for s in evidence)
    assert not any("pg_advisory_xact_lock" in s or "FOR UPDATE" in s for s in statements)
    assert publish.await_count == 2 and all(c.kwargs["scope"] == SCOPE for c in publish.await_args_list)
    assert [c.args[1].payload for c in publish.await_args_list] == [{"operation_id": str(i)} for i in receipt_ids]


# --- progress aggregate -----------------------------------------------------------------------------------

async def test_progress_sql_is_workspace_qualified_and_counts_unavailable_authority_as_failed() -> None:
    captured = {}

    class _Session:
        async def execute(self, statement):
            captured["sql"] = _sql(statement)
            return SimpleNamespace(one=lambda: (1, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0))

    progress = await public.source_cleanup_progress(
        _Session(), OP, source_id=SRC, capture_recorded=True, **SCOPE_KW)
    sql = captured["sql"]
    assert f"document_cleanup_operations.workspace_id = '{WORKSPACE_ID}'" in sql
    assert "membership_revision IS NULL" in sql and "configuration_revision IS NULL" in sql
    assert "membership_revision != 1" in sql and "configuration_revision != 1" in sql
    assert progress.failed_count == 1 and progress.all_required_complete is False


# --- wakeups ---------------------------------------------------------------------------------------------------

def _hint(linked=None, key="aggregate"):
    return public.SourceCleanupWakeup(child_operation_id=OP, workspace_id=WORKSPACE_ID, source_id=SRC,
                                      linked_operation_id=linked, progress_key=key)


def test_wakeup_hint_validates_progress_key() -> None:
    operation = SimpleNamespace(id=OP, workspace_id=WORKSPACE_ID, source_id=SRC, source_purge_operation_id=None)
    assert public.source_cleanup_wakeup_hint(operation, progress_key="raw:1").linked_operation_id is None
    for bad in ("", "UPPER", "x" * 129, "a b"):
        with pytest.raises(ValueError):
            public.source_cleanup_wakeup_hint(operation, progress_key=bad)


def _wake_scope(observer: UUID):
    return InternalJobScope(WORKSPACE_ID, 1, 1, SRC, 4)


async def _wake(observer, hint, *, resolved="ok", listed=True, existing=None, publish_error=None):
    session = MagicMock()
    session.commit, session.rollback = AsyncMock(), AsyncMock()
    scope = _wake_scope(observer) if resolved == "ok" else None
    if resolved == "other_source":
        scope = InternalJobScope(WORKSPACE_ID, 1, 1, uuid4(), 4)
    publish = AsyncMock(side_effect=publish_error)
    with patch.object(public.sources, "resolve_source_purge_job_scope", AsyncMock(return_value=scope)), \
            patch.object(public.sources, "list_source_purge_observer_ids",
                         AsyncMock(return_value=(observer,) if listed else ())), \
            patch.object(ingestion, "get_event_delivery", AsyncMock(return_value=existing)), \
            patch.object(ingestion, "publish_event", publish):
        result = await public.publish_source_cleanup_wakeup(session, hint, observer, multi_workspace_enabled=False)
    return result, publish, session


@pytest.mark.parametrize("case", [{"resolved": None}, {"resolved": "other_source"}, {"listed": False},
                                  {"existing": object()}])
async def test_wakeup_returns_false_without_publishing(case) -> None:
    observer = uuid4()
    result, publish, session = await _wake(observer, _hint(), **case)
    assert result is False
    publish.assert_not_awaited()
    session.commit.assert_not_awaited()


async def test_wakeup_publishes_six_field_payload_with_both_event_id_formulas() -> None:
    linked, other = uuid4(), uuid4()
    result, publish, session = await _wake(linked, _hint(linked=linked))
    assert result is True
    event = publish.await_args.args[1]
    assert event.id == uuid5(OP, "source-purge-progress:aggregate")
    assert event.payload == {
        "operation_id": str(linked), "workspace_id": str(WORKSPACE_ID), "actor_user_id": 1,
        "membership_revision": 1, "source_id": str(SRC), "source_generation": 4}
    assert event.producer == "modules.knowledge.documents" and event.type == "source.purge.progressed"
    session.commit.assert_awaited_once()
    _, publish2, _ = await _wake(other, _hint(linked=linked))
    assert publish2.await_args.args[1].id == uuid5(OP, f"source-purge-progress:{other}:aggregate")


async def test_wakeup_integrity_error_rolls_back_and_never_adopts() -> None:
    result, _, session = await _wake(uuid4(), _hint(), publish_error=IntegrityError("x", {}, Exception()))
    assert result is False
    session.rollback.assert_awaited_once()
    session.commit.assert_not_awaited()

