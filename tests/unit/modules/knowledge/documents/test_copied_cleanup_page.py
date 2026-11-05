"""Characterization tests for ``worker._advance_copied_cleanup`` (one locked copied-owner page).

The routine threads many stage decisions through one locked receipt. These tests pin its
outcomes (return value, receipt field transitions, retry schedule, rollback/requeue/settle/commit
order) so splitting it into stage helpers cannot change behaviour.
"""

from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from modules.knowledge.documents import worker
from tests.unit.modules.knowledge.documents.test_cleanup_worker import (
    ADMITTED,
    EVENT,
    _factory,
    _session,
)


def _receipt(**over):  # type: ignore[no-untyped-def]
    """A fully populated receipt stand-in; defaults mean "everything but chat is already done"."""
    values = {
        "id": uuid4(),
        "source_id": uuid4(),
        "document_id": uuid4(),
        "evidence_scope_status": "captured",
        "status": "queued",
        "error_code": None,
        "raw_status": "succeeded",
        "copied_status": "queued",
        "copied_error_code": None,
        "copied_cursor": None,
        "chat_status": "queued",
        "chat_error_code": None,
        "memory_status": "succeeded",
        "memory_error_code": None,
        "memory_cache_pending": False,
        "memory_cursor": None,
        "memory_unresolved_count": 0,
        "agent_status": "succeeded",
        "agent_error_code": None,
        "agent_cursor": None,
        "agent_unresolved_count": 0,
        "agent_waiting_for_lease": False,
        "materialization_status": "succeeded",
        "materialization_error_code": None,
        "materialization_cursor": None,
        "materialization_unresolved_count": 0,
        "brief_status": "succeeded",
        "brief_error_code": None,
        "brief_cursor": None,
        "brief_unresolved_count": 0,
    }
    values.update(over)
    return SimpleNamespace(**values)


class _Run:
    """Run one page with every collaborator patched; expose the recorded interactions."""

    def __init__(self, hint, locked=None, *, claim=True, settle=True, settled_ok=True) -> None:  # type: ignore[no-untyped-def]
        self.hint = hint
        self.locked = locked if locked is not None else hint
        self.claim = claim
        self.order: list[str] = []
        self.settle = AsyncMock(side_effect=self._settle(settle))
        self.commit = AsyncMock(side_effect=lambda *a, **k: self.order.append("commit"))
        self.requeue = AsyncMock(side_effect=lambda *a, **k: self.order.append("requeue"))
        self.hints = MagicMock(side_effect=lambda hints, op, stage: hints.append(stage))
        self.memory = AsyncMock(return_value=(False, True))
        self.materialization = AsyncMock(return_value=True)
        self.brief = AsyncMock(return_value=True)
        self.evidence = SimpleNamespace(next_cursor=None)
        self.chat_progress = SimpleNamespace(complete=True, next_cursor=None)
        self.agent_preflight = SimpleNamespace(blocked=False)
        self.agent_progress = SimpleNamespace(
            preflight_stale=False,
            unavailable_count=0,
            lease_pending=False,
            complete=True,
            next_cursor=None,
        )
        self.session = _session(self.hint, self.locked)
        self.session.rollback = AsyncMock(side_effect=lambda *a, **k: self.order.append("rollback"))

    def _settle(self, result):  # type: ignore[no-untyped-def]
        async def settle(_session, _admitted, status, _flag, **kwargs):  # type: ignore[no-untyped-def]
            self.order.append(f"settle:{status}")
            self.settled = (status, kwargs.get("next_attempt_at"))
            return result

        return settle

    async def go(self):  # type: ignore[no-untyped-def]
        agents = MagicMock(
            preflight_document_copied_evidence_lease=AsyncMock(return_value=self.agent_preflight),
            purge_document_copied_evidence_page=AsyncMock(return_value=self.agent_progress),
        )
        chat = MagicMock(
            purge_document_copied_evidence_page=AsyncMock(return_value=self.chat_progress)
        )
        self.agents, self.chat = agents, chat
        attempt = worker._Attempt()
        self.attempt = attempt
        with ExitStack() as stack:
            for item in (
                patch.object(worker, "_admit_cleanup", AsyncMock(return_value=ADMITTED)),
                patch.object(
                    worker,
                    "lock_export_privacy_in_uow",
                    AsyncMock(side_effect=lambda *a, **k: self.order.append("privacy")),
                ),
                patch.object(
                    worker,
                    "_lock_claim",
                    AsyncMock(side_effect=lambda *a: self.order.append("claim") or self.claim),
                ),
                patch.object(worker, "_settle", self.settle),
                patch.object(worker, "_commit", self.commit),
                patch.object(worker, "_requeue", self.requeue),
                patch.object(worker, "_hint", self.hints),
                patch.object(worker, "_advance_memory_cleanup", self.memory),
                patch.object(worker, "_advance_materialization_cleanup", self.materialization),
                patch.object(worker, "_advance_brief_cleanup", self.brief),
                patch.object(
                    worker.documents,
                    "list_document_cleanup_evidence_scope",
                    AsyncMock(return_value=self.evidence),
                ),
                patch.dict(
                    "sys.modules", {"modules.agents.public": agents, "modules.chat.public": chat}
                ),
                patch("modules.agents.public", agents, create=True),
                patch("modules.chat.public", chat, create=True),
            ):
                stack.enter_context(item)
            self.result = await worker._advance_copied_cleanup(
                _factory(self.session), False, EVENT, attempt
            )
        return self


def _near(value: datetime | None, delay: timedelta) -> bool:
    return value is not None and abs((value - (datetime.now(UTC) + delay)).total_seconds()) < 5


@pytest.mark.asyncio
async def test_missing_receipt_hint_rolls_back_without_locks() -> None:
    session = _session(None)
    with patch.object(worker, "_admit_cleanup", AsyncMock(return_value=ADMITTED)):
        assert (
            await worker._advance_copied_cleanup(_factory(session), False, EVENT, worker._Attempt())
            is False
        )
    session.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_lost_claim_rolls_back_after_privacy_and_receipt_locks() -> None:
    run = await _Run(_receipt(), claim=False).go()
    assert run.result is False
    assert run.order == ["privacy", "claim", "rollback"]


@pytest.mark.asyncio
async def test_settled_receipt_redelivery_only_resettles_delivered() -> None:
    receipt = _receipt(chat_status="succeeded", status="succeeded")
    run = await _Run(receipt).go()
    assert run.result is True
    assert run.order == ["privacy", "claim", "settle:delivered", "commit"]
    assert run.attempt.progress is None and receipt.status == "succeeded"
    run.chat.purge_document_copied_evidence_page.assert_not_awaited()


@pytest.mark.asyncio
async def test_settled_redelivery_with_moved_claim_rolls_back() -> None:
    run = await _Run(_receipt(chat_status="succeeded", status="succeeded"), settle=False).go()
    assert run.result is False and run.order[-2:] == ["settle:delivered", "rollback"]


@pytest.mark.asyncio
async def test_changed_receipt_between_hint_and_lock_requeues() -> None:
    hint = _receipt()
    locked = _receipt(
        id=hint.id, source_id=hint.source_id, document_id=hint.document_id, agent_status="running"
    )
    run = await _Run(hint, locked).go()
    assert run.result is False
    assert run.order == ["privacy", "claim", "rollback", "requeue"]


@pytest.mark.asyncio
async def test_uncaptured_scope_fails_every_copied_stage_terminally() -> None:
    receipt = _receipt(
        evidence_scope_status="unavailable", memory_status="queued", raw_status="not_present"
    )
    run = await _Run(receipt).go()
    assert run.result is True
    assert (receipt.chat_status, receipt.copied_status, receipt.agent_status) == (
        "failed",
        "failed",
        "failed",
    )
    assert receipt.chat_error_code == receipt.agent_error_code == "evidence_identity_unavailable"
    assert receipt.error_code == "evidence_identity_unavailable"
    assert (receipt.status, receipt.agent_cursor, receipt.agent_waiting_for_lease) == (
        "failed",
        None,
        False,
    )
    assert run.settled[0] == "delivered"  # terminal limitation: no retry scheduled
    assert run.hints.call_args_list[-1].args[2] == "aggregate"
    run.chat.purge_document_copied_evidence_page.assert_not_awaited()


@pytest.mark.asyncio
async def test_chat_page_continues_with_cursor_and_schedules_continuation() -> None:
    receipt = _receipt()
    run = _Run(receipt)
    run.evidence = SimpleNamespace(next_cursor=uuid4())
    await run.go()
    assert run.result is True
    assert receipt.chat_status == "running" and receipt.chat_error_code is None
    assert receipt.copied_cursor == {
        "reference_after": str(run.evidence.next_cursor),
        "chat_cursor": None,
    }
    assert run.settled[0] == "pending" and _near(run.settled[1], worker._CONTINUATION_DELAY)
    assert (receipt.status, receipt.copied_status) == ("running", "running")
    assert run.attempt.stage == "unknown" and run.attempt.progress is None
    run.memory.assert_not_awaited()  # chat is not yet complete


@pytest.mark.asyncio
async def test_incomplete_chat_page_stores_inner_cursor_and_rejects_missing_one() -> None:
    receipt = _receipt()
    run = _Run(receipt)
    run.chat_progress = SimpleNamespace(complete=False, next_cursor="c2")
    await run.go()
    assert receipt.copied_cursor == {"reference_after": None, "chat_cursor": "c2"}
    assert run.settled[0] == "pending"
    bad = _Run(_receipt())
    bad.chat_progress = SimpleNamespace(complete=False, next_cursor=None)
    with pytest.raises(ValueError, match="Chat cleanup page is incomplete"):
        await bad.go()
    malformed = _Run(_receipt(copied_cursor={"chat_cursor": 5}))
    with pytest.raises(ValueError, match="Stored Chat cleanup cursor is malformed"):
        await malformed.go()


@pytest.mark.asyncio
async def test_chat_completion_runs_memory_stage_and_marks_aggregate_success() -> None:
    receipt = _receipt(memory_status="queued")
    run = _Run(receipt)
    run.memory = AsyncMock(return_value=(True, True))
    await run.go()
    assert run.result is True
    assert receipt.chat_status == "succeeded" and receipt.copied_cursor is None
    assert (
        receipt.memory_status == "running" and receipt.memory_error_code is None
    )  # stage helper settles it
    assert run.attempt.evict is True  # memory reported changed rows
    run.memory.assert_awaited_once()
    assert run.settled[0] == "delivered"


@pytest.mark.asyncio
async def test_memory_changes_request_cache_eviction_and_nonterminal_defers() -> None:
    receipt = _receipt(memory_status="queued")
    run = _Run(receipt)
    run.memory = AsyncMock(return_value=(True, False))
    await run.go()
    assert run.attempt.evict is True
    assert run.settled[0] == "pending" and _near(run.settled[1], worker._CONTINUATION_DELAY)


@pytest.mark.asyncio
async def test_all_stages_succeeded_marks_receipt_succeeded_and_clears_errors() -> None:
    receipt = _receipt(
        chat_status="succeeded", status="running", error_code="x", copied_error_code="y"
    )
    run = _Run(receipt)
    # Not a redelivery shortcut: raw is still pending so the page runs, then the aggregate settles.
    receipt.status = "queued"
    receipt.raw_status = "queued"
    receipt.memory_cache_pending = False
    await run.go()
    assert receipt.status == "running"  # raw not complete: aggregate stays running
    done = _receipt(
        chat_status="succeeded",
        status="queued",
        error_code="x",
        copied_error_code="y",
        materialization_status="running",
    )
    run = _Run(done)

    async def finish(_session, operation, **_kw):  # type: ignore[no-untyped-def]
        operation.materialization_status = "succeeded"
        return True

    run.materialization = AsyncMock(side_effect=finish)
    await run.go()
    assert (done.status, done.copied_status, done.copied_error_code, done.error_code) == (
        "succeeded",
        "succeeded",
        None,
        None,
    )
    assert run.hints.call_args_list[-1].args[2] == "aggregate"
    assert run.settled[0] == "delivered"


@pytest.mark.asyncio
async def test_materialization_and_brief_pages_set_stage_and_defer_when_incomplete() -> None:
    receipt = _receipt(
        chat_status="succeeded", materialization_status="running", brief_status="running"
    )
    run = _Run(receipt)
    run.materialization = AsyncMock(return_value=False)
    run.brief = AsyncMock(return_value=False)
    await run.go()
    assert receipt.materialization_error_code is None and receipt.brief_error_code is None
    assert run.settled[0] == "pending" and _near(run.settled[1], worker._CONTINUATION_DELAY)
    assert receipt.status == "running"


@pytest.mark.asyncio
async def test_failure_codes_propagate_to_the_receipt_and_schedule_retries() -> None:
    receipt = _receipt(raw_status="failed", chat_status="succeeded")
    run = await _Run(receipt).go()
    assert receipt.error_code == "file_cleanup_failed" and receipt.status == "failed"
    assert run.settled[0] == "pending" and _near(run.settled[1], worker._RETRY_DELAY)
    # The page resets error_code first, so a prior raw-URI code never suppresses the retry here.
    prior = _receipt(
        raw_status="failed", error_code=worker._RAW_URI_UNAVAILABLE, chat_status="succeeded"
    )
    run = await _Run(prior).go()
    assert prior.error_code == "file_cleanup_failed" and run.settled[0] == "pending"
    brief_failed = _receipt(chat_status="succeeded", brief_status="failed", brief_error_code=None)
    await _Run(brief_failed).go()
    assert (
        brief_failed.copied_error_code == "brief_cleanup_failed" and brief_failed.status == "failed"
    )


@pytest.mark.asyncio
async def test_memory_cache_pending_requests_eviction_and_retry() -> None:
    receipt = _receipt(chat_status="succeeded", memory_cache_pending=True, memory_status="queued")
    run = await _Run(receipt).go()
    assert run.attempt.evict is True
    assert (
        run.memory.await_count == 0
    )  # cache eviction owns the retry; no memory page this delivery
    assert run.settled[0] == "pending" and _near(run.settled[1], worker._RETRY_DELAY)


@pytest.mark.asyncio
async def test_settle_cas_loss_rolls_back_and_clears_attempt_side_effects() -> None:
    receipt = _receipt(memory_cache_pending=True, chat_status="succeeded")
    run = _Run(receipt, settle=False)
    await run.go()
    assert run.result is False
    assert run.order[-2:] == ["settle:pending", "rollback"]
    assert run.attempt.hints == [] and run.attempt.evict is False


@pytest.mark.asyncio
async def test_agent_stage_completes_and_emits_agent_hint() -> None:
    receipt = _receipt(chat_status="succeeded", agent_status="queued")
    run = await _Run(receipt).go()
    assert run.result is True
    assert receipt.agent_status == "succeeded" and receipt.agent_cursor is None
    assert [call.args[2] for call in run.hints.call_args_list][:1] == ["agent"]
    assert run.order == ["privacy", "claim", "settle:delivered", "commit"]


@pytest.mark.asyncio
async def test_agent_stage_unavailable_identities_fail_and_continuation_pages() -> None:
    failed = _receipt(chat_status="succeeded", agent_status="queued")
    run = _Run(failed)
    run.agent_progress.unavailable_count = 2
    await run.go()
    assert (failed.agent_status, failed.agent_error_code) == (
        "failed",
        "evidence_identity_unavailable",
    )
    assert failed.copied_error_code == "evidence_identity_unavailable" and failed.status == "failed"
    paging = _receipt(chat_status="succeeded", agent_status="queued")
    run = _Run(paging)
    run.evidence = SimpleNamespace(next_cursor=uuid4())
    await run.go()
    assert paging.agent_status == "running"
    assert paging.agent_cursor == {
        "v": 1,
        "reference_after": str(run.evidence.next_cursor),
        "candidate_after": None,
        "phase": "discover",
    }
    assert run.settled[0] == "pending"
    inner = _receipt(chat_status="succeeded", agent_status="queued")
    run = _Run(inner)
    run.agent_progress = SimpleNamespace(
        preflight_stale=False,
        unavailable_count=1,
        lease_pending=True,
        complete=False,
        next_cursor="cand",
    )
    await run.go()
    assert inner.agent_cursor == {
        "v": 1,
        "reference_after": None,
        "candidate_after": "cand",
        "phase": "finalize",
    }
    assert inner.agent_waiting_for_lease is True and inner.agent_unresolved_count == 1
    stuck = _Run(_receipt(chat_status="succeeded", agent_status="queued"))
    stuck.agent_progress = SimpleNamespace(
        preflight_stale=False,
        unavailable_count=0,
        lease_pending=False,
        complete=False,
        next_cursor=None,
    )
    with pytest.raises(ValueError, match="Agent cleanup page is incomplete"):
        await stuck.go()


@pytest.mark.asyncio
async def test_agent_preflight_stale_drops_locks_and_requeues() -> None:
    run = _Run(_receipt(chat_status="succeeded", agent_status="queued"))
    run.agent_progress.preflight_stale = True
    await run.go()
    assert run.result is False
    assert run.order[-2:] == ["rollback", "requeue"]


@pytest.mark.asyncio
async def test_agent_preflight_evidence_changes_or_missing_are_handled() -> None:
    receipt = _receipt(
        agent_status="queued",
        agent_cursor={"v": 1, "reference_after": None, "candidate_after": None, "phase": "bad"},
    )
    with pytest.raises(ValueError, match="Stored Agent cleanup cursor is malformed"):
        await _Run(receipt).go()
    run = _Run(_receipt(agent_status="queued"))
    run.evidence = None
    with pytest.raises(ValueError, match="evidence scope is unavailable"):
        await run.go()
