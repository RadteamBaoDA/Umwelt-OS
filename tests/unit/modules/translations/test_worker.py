"""Mock-level T3 runtime contracts: fair claim, fenced slot, per-send checks, fenced publish, no auto-retry."""

import asyncio
import time
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from core.model_gateway.schemas import ModelMapping
from core.workspaces.schemas import WorkspaceContext
from modules.translations import public, worker
from modules.translations.schemas import TranslationBatchRequest, TranslationInput
from modules.translations.service import TranslationBlocked, translate_input

WS = uuid4()
RID = uuid4()


def _sql(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


def _claim(**kw) -> worker.Claim:
    base = {"id": uuid4(), "workspace_id": WS, "actor_user_id": 7, "lease_token": uuid4(), "attempt": 1,
            "resource_type": "daily_brief", "resource_id": RID, "resource_revision": "1", "content_hash": "c" * 64,
            "visibility_hash": "v" * 64, "target_language": "vi", "config_hash": "h" * 64}
    return worker.Claim(**{**base, **kw})


def _run(factory=None) -> worker._Run:
    member = WorkspaceContext(user_id=7, workspace_id=WS, role="member", membership_revision=1)
    return worker._Run(factory or MagicMock(), MagicMock(multi_workspace_enabled=True), MagicMock(), _claim(), member,
                       SimpleNamespace(), None)  # type: ignore[arg-type]


async def test_claim_takes_one_head_per_workspace_least_recently_served_with_skip_locked():
    row = SimpleNamespace(id=uuid4(), workspace_id=WS, actor_user_id=7, attempt_count=0, lease_token=None,
                          lease_expires_at=None, resource_type="news_story", resource_id=RID, resource_revision="r",
                          content_hash="c", visibility_hash="v", target_language="vi", config_hash="h")
    statements = []

    async def scalar(stmt, *a, **k):
        statements.append(_sql(stmt))
        return row

    session = MagicMock(scalar=scalar, flush=AsyncMock())
    claim = await worker.claim_next(session)
    sql = statements[0]
    assert "DISTINCT ON (content_translations.workspace_id)" in sql
    assert "FOR UPDATE SKIP LOCKED" in sql and "NULLS FIRST" in sql
    assert row.attempt_count == 1 and row.lease_token == claim.lease_token and row.lease_expires_at is not None


async def test_claim_returns_none_when_nothing_ready():
    session = MagicMock(scalar=AsyncMock(return_value=None), flush=AsyncMock())
    assert await worker.claim_next(session) is None


async def test_slot_acquire_only_when_free_or_expired_and_bumps_fencing_token():
    seen = []

    async def scalar(stmt, *a, **k):
        seen.append(_sql(stmt))
        return 5

    token = await worker.acquire_slot(MagicMock(scalar=scalar), uuid4())
    assert token == 5
    assert "expires_at IS NULL OR translation_admission_slots.expires_at <" in seen[0]
    assert "fencing_token + 1" in seen[0] and "RETURNING" in seen[0]


async def test_single_holder_second_acquire_gets_none():
    session = MagicMock(scalar=AsyncMock(return_value=None))  # UPDATE ... RETURNING matched no row
    assert await worker.acquire_slot(session, uuid4()) is None


async def test_stale_fencing_token_cannot_renew_or_release():
    result = MagicMock()
    result.first.return_value = None
    session = MagicMock(execute=AsyncMock(return_value=result))
    assert await worker.renew_slot(session, uuid4(), token=3) is False
    sql = _sql(session.execute.await_args.args[0])
    assert "fencing_token = 3" in sql
    await worker.release_slot(session, uuid4(), 3)
    assert "fencing_token = 3" in _sql(session.execute.await_args.args[0])


def _config():
    privacy = SimpleNamespace(allow_remote_reasoning=True, reasoning_destinations=["dest"])
    return SimpleNamespace(
        workspace_id=WS, actor_user_id=1, membership_revision=1, gateway_identity="a" * 64, configuration_revision=1,
        privacy=privacy, aliases={worker.ALIAS: ModelMapping(model="m")}, endpoint_destination_id="dest",
        endpoint_policy_denied=False, omniroute_base_url="http://x", omniroute_api_key="k", request_timeout_seconds=60,
        endpoint_allowed_cidrs=())


def _brief(segments: int) -> TranslationInput:
    lines = ["Zorp " * 300 for _ in range(segments)]  # ~1500 chars each: one segment per line
    return TranslationInput(WS, 7, "daily_brief", RID, "1", {"content": "\n".join(lines)}, "v" * 64, (), False)


class _FakeGateway:
    instances: ClassVar[list["_FakeGateway"]] = []

    def __init__(self, **kw):
        self.kw, self.sent = kw, 0
        _FakeGateway.instances.append(self)

    async def structured(self, alias, mapping, policy, messages, schema, *, before_send, **kw):
        await before_send()
        self.sent += 1
        import json
        payload = json.loads(messages[1]["content"])
        return {"choices": [{"message": {"content": json.dumps(payload)}}]}


async def test_before_send_reruns_for_every_brief_segment(monkeypatch):
    checks = AsyncMock()
    monkeypatch.setattr(worker, "_checked", checks)
    monkeypatch.setattr(worker, "ModelGateway", _FakeGateway)
    run = _run()
    run.config = _config()
    out = await worker._translate(run, _brief(3))
    assert _FakeGateway.instances[-1].sent == 3 and checks.await_count == 3
    assert out["content"].count("\n") == 2


async def test_deadline_aborts_without_partial_brief():
    class Slow(_FakeGateway):
        async def structured(self, *a, **k):
            await asyncio.sleep(0.5)
            return await super().structured(*a, **k)

    cfg = _config()
    policy = SimpleNamespace(reasoning_allowed=True)
    with pytest.raises(TranslationBlocked) as exc:
        await translate_input(Slow(), cfg, policy, _brief(2), "vi", AsyncMock(), deadline=time.monotonic() + 0.1)  # type: ignore[arg-type]
    assert exc.value.code == "deadline_exceeded"


async def test_revoke_between_send_and_publish_gives_blocked(monkeypatch):
    run = _run()
    session = MagicMock(rollback=AsyncMock(), execute=AsyncMock(), commit=AsyncMock())
    run.factory = MagicMock(return_value=MagicMock(__aenter__=AsyncMock(return_value=session), __aexit__=AsyncMock(return_value=False)))
    monkeypatch.setattr(worker, "_verify", AsyncMock(side_effect=TranslationBlocked("stale_request")))
    block = AsyncMock()
    monkeypatch.setattr(worker, "_block", block)
    source = _brief(1)
    await worker._publish(run, source, {"content": "đã dịch"})
    block.assert_awaited_once_with(run.factory, run.claim, "stale_request")
    session.execute.assert_not_awaited()  # nothing written, output discarded


async def test_purged_row_is_not_recreated(monkeypatch):
    run = _run()
    result = MagicMock()
    result.first.return_value = None  # lease-guarded UPDATE matched nothing: the row was purged
    session = MagicMock(execute=AsyncMock(return_value=result), commit=AsyncMock(), add=MagicMock())
    run.factory = MagicMock(return_value=MagicMock(__aenter__=AsyncMock(return_value=session), __aexit__=AsyncMock(return_value=False)))
    monkeypatch.setattr(worker, "_verify", AsyncMock())
    await worker._publish(run, _brief(1), {"content": "đã dịch"})
    stmt = str(session.execute.await_args.args[0].compile(dialect=postgresql.dialect()))
    assert stmt.startswith("UPDATE content_translations") and "lease_token" in stmt and "status" in stmt
    session.add.assert_not_called()


async def test_block_maps_policy_codes_to_blocked_and_model_errors_to_failed():
    seen = {}

    async def finish(factory, claim, **values):
        seen.update(values)
        return True

    original = worker._finish
    worker._finish = finish  # type: ignore[assignment]
    try:
        await worker._block(MagicMock(), _claim(), "privacy_blocked")
        assert seen["status"] == "blocked" and seen["lease_token"] is None
        await worker._block(MagicMock(), _claim(), "invalid_model_output")
        assert seen["status"] == "failed"
    finally:
        worker._finish = original  # type: ignore[assignment]


async def test_second_transport_failure_is_terminal(monkeypatch):
    finish = AsyncMock()
    monkeypatch.setattr(worker, "_finish", finish)
    await worker._retry_or_fail(MagicMock(), _claim(attempt=1), "transport_error")
    assert "status" not in finish.await_args.kwargs and finish.await_args.kwargs["next_attempt_at"] > datetime.now(UTC)
    await worker._retry_or_fail(MagicMock(), _claim(attempt=2), "transport_error")
    assert finish.await_args.kwargs["status"] == "failed"


async def test_failed_row_with_same_fingerprint_is_not_retried_on_resubmit(monkeypatch):
    monkeypatch.setattr(public, "lock_access_fence", AsyncMock())
    monkeypatch.setattr(public, "_AUTHORIZERS", {})

    async def ok(session, *, scope, resource_id, multi_workspace_enabled):
        return public.ResourceAuthorization("r1", "c" * 64, "v" * 64)

    public.register_resource_authorizer("news_story", ok)
    scope = WorkspaceContext(user_id=7, workspace_id=WS, role="member", membership_revision=1)
    settings = SimpleNamespace(enabled=True, target_language="vi", configuration_revision=1)
    item_id = uuid4()
    config = public.config_hash_for(
        workspace_id=WS, actor_user_id=7, resource_revision="r1", content_hash="c" * 64, visibility_hash="v" * 64,
        target_language="vi", settings_revision=1, policy_fingerprint="")
    failed = SimpleNamespace(
        id=uuid4(), resource_type="news_story", resource_id=item_id, resource_revision="r1", content_hash="c" * 64,
        visibility_hash="v" * 64, config_hash=config, status="failed", error_code="transport_error", result=None,
        expires_at=datetime(2999, 1, 1, tzinfo=UTC))
    session = MagicMock(scalar=AsyncMock(return_value=settings), execute=AsyncMock(), flush=AsyncMock())
    session.scalars = AsyncMock(return_value=MagicMock(all=lambda: [failed]))
    request = TranslationBatchRequest.model_validate({"items": [
        {"resource_type": "news_story", "resource_id": str(item_id), "resource_revision": "r1"}]})
    out = await public.request_translations(session, scope, request, multi_workspace_enabled=True)
    assert failed.status == "failed" and out.enqueue_ids == [] and out.items[0].status == "failed"
    assert "enqueue_ids" not in out.model_dump()


def test_jobs_and_crons_are_registered():
    from apps.worker.main import WorkerSettings

    names = {getattr(f, "name", getattr(f, "__name__", "")) for f in WorkerSettings.functions}
    assert "translate_content" in names
    cron_names = {getattr(c, "name", "") for c in WorkerSettings.cron_jobs}
    assert any("recover_translation_jobs" in n for n in cron_names)
    assert any("sweep_translation_orphans" in n for n in cron_names)
    assert any("expire_translations" in n for n in cron_names)
