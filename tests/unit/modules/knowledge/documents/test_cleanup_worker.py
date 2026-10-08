"""Documents cleanup worker: original-authority admission, ordered locks, scoped recovery and wakeups."""

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from core.workspaces.schemas import AccessFence, InternalJobScope
from modules.knowledge.documents import public, worker
from modules.knowledge.documents.schemas import DocumentCleanupJobIdentity

WS, SRC, OP, EVENT = uuid4(), uuid4(), uuid4(), uuid4()
CLAIM = datetime(2026, 1, 1, tzinfo=UTC)
JOB_SCOPE = InternalJobScope(
    workspace_id=WS, actor_user_id=7, membership_revision=3, source_id=SRC, source_generation=5,
)
ORIGINAL = AccessFence(WS, 7, 3, 2)
IDENTITY = DocumentCleanupJobIdentity(
    operation_id=OP, workspace_id=WS, actor_user_id=7, membership_revision=3, configuration_revision=2,
    source_id=SRC, source_generation=5, document_id=uuid4(),
)
ADMITTED = worker._Admitted(IDENTITY, JOB_SCOPE, ORIGINAL, EVENT, CLAIM)


def _factory(*sessions):
    queue = list(sessions) or [_session()]
    contexts = []
    for session in queue:
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=session)
        context.__aexit__ = AsyncMock(return_value=None)
        contexts.append(context)
    return MagicMock(side_effect=contexts)


def _session(*scalars):
    session = MagicMock()
    session.scalar = AsyncMock(side_effect=list(scalars))
    session.commit, session.rollback = AsyncMock(), AsyncMock()
    return session


def _sql(statement) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


def _raw_receipt(**overrides):
    values = {"id": OP, "raw_uri": "u/raw.bin", "raw_status": "queued", "error_code": None}
    return SimpleNamespace(**{**values, **overrides})


class _Calls:
    """Record the raw-stage lock sequence across all patched collaborators."""

    def __init__(self) -> None:
        self.order: list[str] = []

    def mock(self, name, **kwargs):
        async def record(*_a, **_k):
            self.order.append(name)
            return kwargs.get("result")
        return AsyncMock(side_effect=record)


async def test_resolver_none_means_no_receipt_read_no_uri_lock_no_owner_call() -> None:
    session = _session()
    with patch.object(worker.ingestion, "read_document_cleanup_event_operation_id", AsyncMock(return_value=OP)), \
            patch.object(worker.ingestion, "resolve_ingestion_event_scope", AsyncMock(return_value=None)), \
            patch.object(worker.documents, "lock_raw_uri_identity", AsyncMock()) as uri_lock, \
            patch.object(worker, "lock_export_privacy_in_uow", AsyncMock()) as privacy:
        proceed = await worker._advance_raw_document_cleanup(_factory(session), MagicMock(), False, EVENT)
    assert proceed is False
    session.scalar.assert_not_called()  # no select on DocumentCleanupOperation before admission
    uri_lock.assert_not_awaited()
    privacy.assert_not_awaited()


async def test_stale_configuration_is_not_admitted() -> None:
    event = SimpleNamespace(status="queued", dispatched_at=CLAIM)
    session = _session()
    with patch.object(worker.ingestion, "read_document_cleanup_event_operation_id", AsyncMock(return_value=OP)), \
            patch.object(worker.ingestion, "resolve_ingestion_event_scope", AsyncMock(return_value=JOB_SCOPE)), \
            patch.object(worker.ingestion, "get_document_cleanup_event", AsyncMock(return_value=event)), \
            patch.object(worker.documents, "read_document_cleanup_job_identity", AsyncMock(return_value=IDENTITY)), \
            patch.object(worker, "read_access_fence", AsyncMock(return_value=AccessFence(WS, 7, 3, 99))):
        assert await worker._admit_cleanup(session, EVENT, False) is None
    for status, claim in (("pending", CLAIM), ("queued", None)):  # not a claimed queued event
        event = SimpleNamespace(status=status, dispatched_at=claim)
        with patch.object(worker.ingestion, "read_document_cleanup_event_operation_id", AsyncMock(return_value=OP)), \
                patch.object(worker.ingestion, "resolve_ingestion_event_scope", AsyncMock(return_value=JOB_SCOPE)), \
                patch.object(worker.ingestion, "get_document_cleanup_event", AsyncMock(return_value=event)):
            assert await worker._admit_cleanup(session, EVENT, False) is None


async def test_authorization_loss_is_not_admitted() -> None:
    with patch.object(worker.ingestion, "read_document_cleanup_event_operation_id", AsyncMock(return_value=OP)), \
            patch.object(worker.ingestion, "resolve_ingestion_event_scope",
                         AsyncMock(side_effect=HTTPException(403, "gone"))):
        assert await worker._admit_cleanup(_session(), EVENT, False) is None


def _raw_patches(receipt, *, claim=True, referenced=False, calls=None):
    calls = calls or _Calls()
    return calls, [
        patch.object(worker, "_admit_cleanup", AsyncMock(return_value=ADMITTED)),
        patch.object(worker, "lock_export_privacy_in_uow", calls.mock("privacy")),
        patch.object(worker.documents, "lock_raw_uri_identity", calls.mock("uri")),
        patch.object(worker, "_lock_claim", AsyncMock(side_effect=lambda *a: calls.order.append("event") or claim)),
        patch.object(worker.documents, "raw_uri_is_referenced_for_cleanup",
                     referenced if isinstance(referenced, AsyncMock) else AsyncMock(return_value=referenced)),
        patch.object(worker, "_commit", AsyncMock()),
        patch.object(worker, "_publish_wakeups", AsyncMock()),
        patch.object(worker, "_hint", MagicMock()),
    ]


async def _run_raw(receipt, session=None, **kwargs):
    session = session or _session(receipt, receipt)
    calls, patches = _raw_patches(receipt, **kwargs)
    unlink = MagicMock()
    path = MagicMock()
    path.unlink = unlink
    for item in patches:
        item.start()
    try:
        with patch.object(worker, "storage_path", MagicMock(return_value=path)):
            proceed = await worker._advance_raw_document_cleanup(
                _factory(session), SimpleNamespace(data_dir="d"), False, EVENT,
            )
    finally:
        for item in reversed(patches):
            item.stop()
    return proceed, calls, unlink, session


async def test_raw_stage_lock_order_privacy_uri_receipt_event() -> None:
    receipt = _raw_receipt()
    proceed, calls, unlink, session = await _run_raw(receipt)
    assert proceed is True
    assert calls.order == ["privacy", "uri", "event"]
    # hint read (privacy first) precedes the FOR UPDATE receipt read; both carry workspace/actor predicates
    first, second = (_sql(call.args[0]) for call in session.scalar.await_args_list)
    assert "FOR UPDATE" not in first and "FOR UPDATE" in second
    assert "workspace_id" in first and "actor_user_id" in second
    assert receipt.raw_status == "succeeded"
    unlink.assert_called_once()


async def test_raw_shared_uri_is_retained_without_unlink() -> None:
    receipt = _raw_receipt()
    _, _, unlink, _ = await _run_raw(receipt, referenced=True)
    assert receipt.raw_status == "retained_shared"
    unlink.assert_not_called()


@pytest.mark.parametrize("failure", [ValueError("traversal"), OSError("io"), HTTPException(409, "stale")])
async def test_raw_helper_error_fails_without_unlink(failure) -> None:
    receipt = _raw_receipt()
    _, _, unlink, _ = await _run_raw(receipt, referenced=AsyncMock(side_effect=failure))
    assert (receipt.raw_status, receipt.error_code) == ("failed", "file_cleanup_failed")
    unlink.assert_not_called()


async def test_raw_traversal_uri_fails_closed() -> None:
    receipt = _raw_receipt(raw_uri="../../etc/passwd")
    _, patches = _raw_patches(receipt)
    session = _session(receipt, receipt)
    for item in patches:
        item.start()
    try:
        with patch.object(worker, "storage_path", MagicMock(side_effect=ValueError("outside"))):
            await worker._advance_raw_document_cleanup(_factory(session), SimpleNamespace(data_dir="d"), False, EVENT)
    finally:
        for item in reversed(patches):
            item.stop()
    assert (receipt.raw_status, receipt.error_code) == ("failed", "raw_uri_unavailable")


async def test_claim_moved_at_relock_has_no_effects() -> None:
    receipt = _raw_receipt()
    proceed, _, unlink, session = await _run_raw(receipt, claim=False)
    assert proceed is False
    assert receipt.raw_status == "queued"
    unlink.assert_not_called()
    session.rollback.assert_awaited()


async def test_uri_changed_between_hint_and_lock_has_no_effects() -> None:
    proceed, _, unlink, _ = await _run_raw(None, session=_session(_raw_receipt(), _raw_receipt(raw_uri="other")))
    assert proceed is False
    unlink.assert_not_called()


async def test_lock_claim_requires_original_queued_claim() -> None:
    for event, expected in (
        (SimpleNamespace(status="queued", dispatched_at=CLAIM), True),
        (SimpleNamespace(status="queued", dispatched_at=datetime.now(UTC)), False),
        (SimpleNamespace(status="pending", dispatched_at=CLAIM), False),
        (None, False),
    ):
        with patch.object(worker.ingestion, "lock_document_cleanup_event_in_uow", AsyncMock(return_value=event)):
            assert await worker._lock_claim(MagicMock(), ADMITTED, False) is expected


def _copied_receipt(**overrides):
    values = {
        "id": OP, "source_id": SRC, "document_id": uuid4(), "evidence_scope_status": "captured",
        "agent_status": "queued", "agent_cursor": None,
    }
    return SimpleNamespace(**{**values, **overrides})


async def test_agent_preflight_after_admission_and_blocked_defers_without_privacy_lock() -> None:
    receipt = _copied_receipt()
    session = _session(receipt)
    evidence = SimpleNamespace()
    preflight = AsyncMock(return_value=SimpleNamespace(blocked=True))
    agents = MagicMock(preflight_document_copied_evidence_lease=preflight)
    admit = AsyncMock(return_value=ADMITTED)
    with patch.object(worker, "_admit_cleanup", admit), \
            patch.object(worker, "_attempt_progress_snapshot", return_value=("p",)), \
            patch.object(worker.documents, "list_document_cleanup_evidence_scope", AsyncMock(return_value=evidence)), \
            patch.dict("sys.modules", {"modules.agents.public": agents}), \
            patch("modules.agents.public", agents, create=True), \
            patch.object(worker, "lock_export_privacy_in_uow", AsyncMock()) as privacy, \
            patch.object(worker, "_settle", AsyncMock(return_value=True)) as settle, \
            patch.object(worker, "_commit", AsyncMock()) as commit:
        attempt = worker._Attempt()
        committed = await worker._advance_copied_cleanup(_factory(session), False, EVENT, attempt)
    assert committed is False
    admit.assert_awaited_once()  # admission precedes any receipt/agent work
    preflight.assert_awaited_once()
    assert preflight.await_args.kwargs["scope"] == JOB_SCOPE
    privacy.assert_not_awaited()
    assert settle.await_args.args[2] == "pending"
    commit.assert_awaited_once()


async def test_copied_stage_not_admitted_performs_no_receipt_read() -> None:
    session = _session()
    with patch.object(worker, "_admit_cleanup", AsyncMock(return_value=None)):
        assert await worker._advance_copied_cleanup(_factory(session), False, EVENT, worker._Attempt()) is False
    session.scalar.assert_not_called()


class _Receipt:
    """ORM stand-in that proves nothing reads receipt attributes after the rollback."""

    def __init__(self) -> None:
        self.rolled_back = False
        self.raw_status = "queued"
        self.chat_status = "queued"
        self.agent_status = "succeeded"
        self.memory_status = "queued"
        self.memory_error_code = None
        self.copied_status = self.copied_error_code = self.status = self.error_code = None
        self.chat_error_code = None
        self.copied_cursor = None

    def __getattribute__(self, name):
        if name != "rolled_back" and object.__getattribute__(self, "rolled_back"):
            raise AssertionError(f"ORM attribute {name} read after rollback")
        return object.__getattribute__(self, name)


async def _recover(receipt, *, progress, snapshot_value, admitted=ADMITTED, stage="chat", malformed=False):
    session = _session(receipt)
    session.rollback = AsyncMock(side_effect=lambda: setattr(receipt, "rolled_back", True))
    attempt = worker._Attempt(admitted=ADMITTED, progress=progress, stage=stage)
    settle = AsyncMock(return_value=True)
    with patch.object(worker, "_admit_cleanup", AsyncMock(return_value=admitted)), \
            patch.object(worker, "lock_export_privacy_in_uow", AsyncMock()), \
            patch.object(worker, "_attempt_progress_snapshot", return_value=snapshot_value), \
            patch.object(worker, "_lock_claim", AsyncMock(return_value=True)), \
            patch.object(worker, "_hint", MagicMock()), \
            patch.object(worker, "_settle", settle), \
            patch.object(worker, "_commit", AsyncMock()) as commit, \
            patch.object(worker, "_publish_wakeups", AsyncMock()):
        await worker._recover_attempt(_factory(session), False, EVENT, attempt, malformed=malformed)
    return settle, commit, session


async def test_recovery_with_changed_snapshot_writes_nothing() -> None:
    receipt = _Receipt()
    settle, commit, session = await _recover(receipt, progress=("a",), snapshot_value=("b",))
    settle.assert_not_awaited()
    commit.assert_not_awaited()
    session.rollback.assert_awaited_once()  # and the _Receipt raised on no post-rollback attribute read


async def test_recovery_with_changed_admission_writes_nothing() -> None:
    other = worker._Admitted(IDENTITY, JOB_SCOPE, ORIGINAL, EVENT, datetime.now(UTC))
    settle, commit, _ = await _recover(_Receipt(), progress=("a",), snapshot_value=("a",), admitted=other)
    settle.assert_not_awaited()
    commit.assert_not_awaited()


@pytest.mark.parametrize(("malformed", "code"), [(True, "chat_cursor_reset"), (False, "chat_cleanup_failed")])
async def test_recovery_with_matching_snapshot_writes_stage_error_and_cas_settles(malformed, code) -> None:
    receipt = _Receipt()
    settle, commit, _ = await _recover(receipt, progress=("a",), snapshot_value=("a",), malformed=malformed)
    assert (receipt.chat_status, receipt.chat_error_code, receipt.status) == ("failed", code, "failed")
    assert settle.await_args.args[2] == "pending"
    commit.assert_awaited_once()


async def test_recovery_without_snapshot_is_no_mutation() -> None:
    attempt = worker._Attempt(admitted=ADMITTED, progress=None)
    factory = _factory()
    await worker._recover_attempt(factory, False, EVENT, attempt, malformed=False)
    factory.assert_not_called()


async def test_reconcilers_use_one_distinct_session_per_id_and_skip_unadmitted() -> None:
    ids = (uuid4(), uuid4(), uuid4())
    listing = _session()
    factory = _factory(listing, *[_session() for _ in ids])
    opened = AsyncMock(side_effect=[True, False, True])
    ctx = {"session_factory": factory, "settings": SimpleNamespace(multi_workspace_enabled=False)}
    with patch.object(worker.documents, "pending_document_memory_cleanup_ids", AsyncMock(return_value=ids)), \
            patch.object(worker, "_reopen_cleanup_event", opened):
        assert await worker.reconcile_document_memory_cleanup(ctx) == 2
    assert opened.await_count == 3
    assert factory.call_count == 1  # listing session; per-ID sessions are opened by the reopen helper


async def test_reopen_publishes_exact_envelope_under_receipt_scope_and_skips_legacy() -> None:
    session = _session()
    with patch.object(worker.documents, "resolve_document_cleanup_job_identity", AsyncMock(return_value=None)):
        assert await worker._reopen_cleanup_event(_factory(session), False, OP, datetime.now(UTC)) is False
    publish = AsyncMock()
    session = _session()
    with patch.object(worker.documents, "resolve_document_cleanup_job_identity", AsyncMock(return_value=IDENTITY)), \
            patch.object(worker.ingestion, "get_document_cleanup_event", AsyncMock(return_value=None)), \
            patch.object(worker.ingestion, "publish_event", publish), \
            patch.object(worker, "commit_with_replay", AsyncMock()) as commit:
        assert await worker._reopen_cleanup_event(_factory(session), False, OP, datetime.now(UTC)) is True
    event = publish.await_args.args[1]
    assert event.payload == {"operation_id": str(OP)} and publish.await_args.kwargs["scope"] == JOB_SCOPE
    assert commit.await_args.kwargs["access_fence"] == ORIGINAL


async def test_reopen_failed_quarantined_event_cas_on_status_and_claim() -> None:
    event = SimpleNamespace(status="failed", dispatched_at=CLAIM)
    settle = AsyncMock(return_value=True)
    with patch.object(worker.documents, "resolve_document_cleanup_job_identity", AsyncMock(return_value=IDENTITY)), \
            patch.object(worker.ingestion, "get_document_cleanup_event", AsyncMock(return_value=event)), \
            patch.object(worker.ingestion, "settle_document_cleanup_event_in_uow", settle), \
            patch.object(worker, "commit_with_replay", AsyncMock()):
        assert await worker._reopen_cleanup_event(_factory(_session()), False, OP, datetime.now(UTC)) is True
    assert settle.await_args.args[2] == "pending"
    assert settle.await_args.kwargs["expected_status"] == "failed"
    assert settle.await_args.kwargs["dispatched_at"] == CLAIM


async def test_reopen_denied_http_skips_but_other_errors_raise() -> None:
    for code, raises in ((403, False), (409, False), (500, True)):
        with patch.object(worker.documents, "resolve_document_cleanup_job_identity",
                          AsyncMock(side_effect=HTTPException(code, "x"))):
            if raises:
                with pytest.raises(HTTPException):
                    await worker._reopen_cleanup_event(_factory(_session()), False, OP, datetime.now(UTC))
            else:
                assert await worker._reopen_cleanup_event(_factory(_session()), False, OP, datetime.now(UTC)) is False


async def test_reconciler_queries_exclude_legacy_null_authority_and_stay_bounded() -> None:
    class Capture:
        def __init__(self) -> None:
            self.statements = []

        async def scalars(self, statement):
            self.statements.append(statement)
            return SimpleNamespace(all=list)

    capture = Capture()
    await public.pending_document_memory_cleanup_ids(capture, limit=100)
    await public.pending_document_agent_cleanup_ids(capture, limit=100)
    await public.pending_document_copied_stage_cleanup_ids(capture, limit=100)
    for statement in capture.statements:
        sql = _sql(statement)
        assert "membership_revision IS NOT NULL" in sql and "LIMIT" in sql
    with pytest.raises(ValueError, match="between 1 and 100"):
        await public.pending_document_agent_cleanup_ids(capture, limit=101)


async def test_wakeups_publish_per_observer_in_separate_sessions_after_commit() -> None:
    linked, other = uuid4(), uuid4()
    hint = SimpleNamespace(linked_operation_id=linked, workspace_id=WS, source_id=SRC)
    discovery, first, second = _session(), _session(), _session()
    factory = _factory(discovery, first, second)
    publish = AsyncMock(side_effect=[True, RuntimeError("deferred")])
    with patch.object(worker.sources, "discover_source_purge_observer_ids",
                      AsyncMock(return_value=(linked, other))) as discover, \
            patch.object(worker.documents, "publish_source_cleanup_wakeup", publish):
        await worker._publish_wakeups(factory, False, [hint])  # a failing observer never propagates
    assert factory.call_count == 3  # discovery + one fresh session per deduplicated observer
    assert discover.await_args.kwargs["limit"] == 100
    assert [call.args[2] for call in publish.await_args_list] == [linked, other]
    assert publish.await_args_list[0].args[0] is first and publish.await_args_list[1].args[0] is second


async def test_cleanup_is_not_module_gated() -> None:
    # Cleanup must finish even for a disabled module: the worker never consults the module gate.
    source = Path(worker.__file__).read_text(encoding="utf-8")
    assert "module_is_enabled" not in source.replace("``module_is_enabled``", "")
    with patch("modules.settings.public.module_is_enabled", AsyncMock()) as gate, \
            patch.object(worker, "_admit_cleanup", AsyncMock(return_value=None)):
        ctx = {"session_factory": _factory(_session()), "settings": SimpleNamespace(multi_workspace_enabled=False),
               "redis": MagicMock()}
        await worker.process_document_cleanup(ctx, str(EVENT))
    gate.assert_not_awaited()
