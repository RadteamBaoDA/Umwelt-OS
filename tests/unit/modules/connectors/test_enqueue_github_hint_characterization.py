"""Characterization tests pinning ``enqueue_github_hint`` outcomes, row writes and lock/commit order.

The function was split into phase helpers; these tests drive the public entry point only, so they
hold the same contract before and after the split: which rows are locked in which order, which
result is returned for each branch, and when the transaction commits or rolls back.
"""

import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from core.errors import ConflictError, ValidationFailedError
from core.workspaces.schemas import AccessFence, InternalJobScope
from modules.connectors import public as connectors
from modules.connectors.github.webhooks import GitHubTargetHint
from modules.connectors.models import GithubSourceHint

WORKSPACE_ID = uuid4()
SOURCE_ID = uuid4()
DELIVERY_ID = uuid4()
SCOPE = InternalJobScope(
    workspace_id=WORKSPACE_ID,
    actor_user_id=7,
    membership_revision=3,
    source_id=SOURCE_ID,
    source_generation=2,
)
FENCE = AccessFence(WORKSPACE_ID, 7, 3, 5)
_CONFIG = {
    "github_owner": "octo",
    "github_repository": "repo",
    "include_issues": True,
    "include_pulls": True,
    "include_commits": True,
    "include_releases": True,
}
HINTS = "modules.connectors.service.github_hints"


def _binding() -> connectors.GitHubEventBinding:
    return connectors.GitHubEventBinding(
        workspace_id=WORKSPACE_ID,
        actor_user_id=7,
        membership_revision=3,
        workspace_configuration_revision=5,
        source_id=SOURCE_ID,
        source_status="active",
        source_generation=2,
        connector_revision=4,
        grant_state="ready",
        grant_expires_at=datetime.now(UTC) + timedelta(hours=1),
        repository_id="11",
        installation_id="22",
        app_id="33",
        binding_revision=6,
        resource_scope=("issue", "pull"),
    )


def _target(**over: str) -> GitHubTargetHint:
    values = {
        "resource": "issue",
        "locator_kind": "number",
        "locator": "9",
        "intent": "refresh",
    } | over
    return GitHubTargetHint(**values)  # type: ignore[arg-type]


class _Session:
    """Scripted session recording the order of reads, locks, adds and the commit/rollback calls."""

    def __init__(
        self,
        scalars: list[object],
        delivery: object | None = None,
        many: list[object] | None = None,
    ) -> None:
        self.queue = list(scalars)
        self.calls: list[str] = []
        self.added: list[object] = []
        self.delivery = delivery
        self.many = many or []

    async def scalar(self, stmt: object) -> object:
        match = re.search(r"FROM (\w+)", str(stmt))
        self.calls.append(f"scalar:{match.group(1) if match else 'expr'}")
        return self.queue.pop(0)

    async def scalars(self, stmt: object) -> list[object]:
        match = re.search(r"FROM (\w+)", str(stmt))
        self.calls.append(f"scalars:{match.group(1) if match else 'expr'}")
        return self.many

    async def get(self, model: object, key: object) -> object | None:
        self.calls.append("get")
        return self.delivery

    def add(self, row: object) -> None:
        self.calls.append("add")
        self.added.append(row)

    async def commit(self) -> None:
        self.calls.append("commit")

    async def rollback(self) -> None:
        self.calls.append("rollback")


def _page(binding: connectors.GitHubEventBinding, admissions: tuple = ()) -> dict[str, object]:
    fanout = connectors._GitHubFanoutBinding.model_validate(binding.model_dump(mode="json"))
    return connectors._GitHubFanoutPage(
        cursor_before=None,
        cursor_after=None,
        bindings=(fanout,),
        admissions=admissions,
    ).model_dump(mode="json")


def _fixtures(
    target: GitHubTargetHint, *, status: str = "active", grant_state: str = "ready"
) -> SimpleNamespace:
    binding = _binding()
    source = SimpleNamespace(
        id=SOURCE_ID,
        provider="github",
        status=status,
        generation=2,
        configuration=dict(_CONFIG),
    )
    provisioned = SimpleNamespace(source_generation=2, desired_revision=4)
    grant = SimpleNamespace(
        repository_id="11",
        installation_id="22",
        app_id="33",
        binding_revision=6,
        configuration_revision=4,
        state=grant_state,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    outbox = SimpleNamespace(
        state="pending",
        capacity_reserved=False,
        binding_cursor=None,
        fanout_page=_page(binding),
    )
    delivery = SimpleNamespace(targets=[target.model_dump(mode="json")])
    return SimpleNamespace(
        binding=binding,
        source=source,
        provisioned=provisioned,
        grant=grant,
        outbox=outbox,
        delivery=delivery,
    )


async def _run(
    session: _Session, fx: SimpleNamespace, target: GitHubTargetHint, *, pause_source=None
):
    locked = SimpleNamespace(workspace_id=WORKSPACE_ID)
    pause = pause_source or AsyncMock(
        return_value=SimpleNamespace(id=SOURCE_ID, generation=3, status="paused")
    )
    with (
        patch(f"{HINTS}._connector_access", AsyncMock(return_value=FENCE)),
        patch("modules.sources.public.lock_source", AsyncMock(return_value=locked)),
        patch("modules.sources.public.get_connector_source", AsyncMock(return_value=fx.source)),
        patch(
            "modules.sources.public.prepare_source_pause_for_connector_in_uow", AsyncMock()
        ) as prepare,
        patch("modules.sources.public.pause_source_for_connector_in_uow", pause),
        patch("core.realtime.commit_with_replay", AsyncMock()) as replay,
    ):
        result = await connectors.enqueue_github_hint(
            session,
            binding=fx.binding,
            delivery_receipt_id=DELIVERY_ID,
            target=target,  # type: ignore[arg-type]
            expected_binding_cursor=None,
            scope=SCOPE,
            multi_workspace_enabled=False,
        )
    return result, prepare, pause, replay


ROWS = [
    "scalar:connector_provisioning",
    "scalar:github_oauth_grants",
    "scalar:github_webhook_outbox",
    "get",
]


@pytest.mark.asyncio
async def test_new_hint_is_created_pending_and_result_recorded_on_page() -> None:
    target = _target()
    fx = _fixtures(target)
    session = _Session(
        [fx.provisioned, fx.grant, fx.outbox, None, 0, SimpleNamespace(pending_count=0)],
        fx.delivery,
    )
    result, prepare, _pause, _replay = await _run(session, fx, target)
    assert (
        result.disposition == "pending"
        and result.hint_id is not None
        and result.dirty_revision == 1
    )
    assert session.calls == [
        *ROWS,
        "scalar:github_source_hints",
        "scalar:github_source_hints",
        "scalar:github_webhook_capacity",
        "add",
        "commit",
    ]
    hint = session.added[0]
    assert isinstance(hint, GithubSourceHint)
    assert (hint.state, hint.intent, hint.locator, hint.capacity_reserved) == (
        "pending",
        "refresh",
        "9",
        True,
    )
    assert hint.last_delivery_id == DELIVERY_ID and hint.binding_revision == 6
    prepare.assert_not_awaited()
    stored = fx.outbox.fanout_page["admissions"]
    assert len(stored) == 1 and stored[0]["result"]["disposition"] == "pending"


@pytest.mark.asyncio
async def test_existing_hint_is_coalesced_and_rearmed() -> None:
    target = _target()
    fx = _fixtures(target)
    existing = SimpleNamespace(
        id=uuid4(),
        capacity_reserved=True,
        dirty_revision=2,
        reconcile_page=4,
        last_delivery_id=None,
        intent="x",
        state="completed",
    )
    session = _Session([fx.provisioned, fx.grant, fx.outbox, existing], fx.delivery)
    result, *_ = await _run(session, fx, target)
    assert (result.disposition, result.hint_id, result.dirty_revision) == (
        "coalesced",
        existing.id,
        3,
    )
    assert session.calls == [*ROWS, "scalar:github_source_hints", "commit"]
    assert (existing.state, existing.intent, existing.reconcile_page) == ("pending", "refresh", 4)
    assert existing.last_delivery_id == DELIVERY_ID and existing.binding_revision == 6


@pytest.mark.asyncio
async def test_existing_hint_without_slot_reserves_capacity_or_reports_exhausted() -> None:
    target = _target()
    fx = _fixtures(target)
    existing = SimpleNamespace(
        id=uuid4(), capacity_reserved=False, dirty_revision=1, reconcile_page=1
    )
    capacity = SimpleNamespace(pending_count=100_000)
    session = _Session([fx.provisioned, fx.grant, fx.outbox, existing, capacity], fx.delivery)
    result, *_ = await _run(session, fx, target)
    assert result.disposition == "capacity_exhausted" and result.hint_id is None
    assert session.calls[-2:] == ["scalar:github_webhook_capacity", "rollback"]
    assert existing.dirty_revision == 1 and existing.capacity_reserved is False

    fx2 = _fixtures(target)
    existing2 = SimpleNamespace(
        id=uuid4(), capacity_reserved=False, dirty_revision=1, reconcile_page=1
    )
    capacity2 = SimpleNamespace(pending_count=5)
    session2 = _Session(
        [fx2.provisioned, fx2.grant, fx2.outbox, existing2, capacity2], fx2.delivery
    )
    result2, *_ = await _run(session2, fx2, target)
    assert (
        result2.disposition == "coalesced"
        and capacity2.pending_count == 6
        and existing2.capacity_reserved is True
    )


@pytest.mark.asyncio
async def test_hint_overflow_collapses_to_repository_reconcile() -> None:
    target = _target()
    fx = _fixtures(target)
    existing = SimpleNamespace(
        id=uuid4(), capacity_reserved=True, dirty_revision=1, reconcile_page=7
    )
    session = _Session([fx.provisioned, fx.grant, fx.outbox, None, 100, existing], fx.delivery)
    result, *_ = await _run(session, fx, target)
    assert (
        result.disposition == "coalesced"
        and existing.intent == "reconcile"
        and existing.reconcile_page == 1
    )
    assert session.calls[-4:] == [
        "scalar:github_source_hints",
        "scalar:github_source_hints",
        "scalar:github_source_hints",
        "commit",
    ]

    fx2 = _fixtures(target)
    session2 = _Session(
        [fx2.provisioned, fx2.grant, fx2.outbox, None, 100, None, SimpleNamespace(pending_count=0)],
        fx2.delivery,
    )
    result2, *_ = await _run(session2, fx2, target)
    assert result2.disposition == "pending"
    hint = session2.added[0]
    assert (hint.locator_kind, hint.locator, hint.intent) == ("repository", "11", "reconcile")


@pytest.mark.asyncio
async def test_new_hint_without_capacity_row_is_capacity_exhausted() -> None:
    target = _target()
    fx = _fixtures(target)
    session = _Session([fx.provisioned, fx.grant, fx.outbox, None, 0, None], fx.delivery)
    result, *_ = await _run(session, fx, target)
    assert result.disposition == "capacity_exhausted"
    assert session.calls[-1] == "rollback" and session.added == []


@pytest.mark.asyncio
async def test_stale_binding_and_ignored_resource_are_terminal_and_committed() -> None:
    target = _target()
    fx = _fixtures(target)
    fx.grant.binding_revision = 99
    session = _Session([fx.provisioned, fx.grant, fx.outbox], fx.delivery)
    result, *_ = await _run(session, fx, target)
    assert result.disposition == "stale_binding" and session.calls == [*ROWS, "commit"]
    assert fx.outbox.fanout_page["admissions"][0]["result"]["disposition"] == "stale_binding"

    fx_bad = _fixtures(target)
    fx_bad.source.configuration = {}
    session_bad = _Session([fx_bad.provisioned, fx_bad.grant, fx_bad.outbox], fx_bad.delivery)
    result_bad, *_ = await _run(session_bad, fx_bad, target)
    assert result_bad.disposition == "stale_binding" and session_bad.calls == [*ROWS, "commit"]

    release = _target(resource="release", locator_kind="release_id", locator="5")
    fx2 = _fixtures(release)
    fx2.source.configuration = {**_CONFIG, "include_releases": False}
    session2 = _Session([fx2.provisioned, fx2.grant, fx2.outbox], fx2.delivery)
    result2, *_ = await _run(session2, fx2, release)
    assert result2.disposition == "ignored" and session2.calls == [*ROWS, "commit"]


@pytest.mark.asyncio
async def test_stale_progress_when_outbox_missing_cursor_changed_or_binding_not_in_page() -> None:
    target = _target()
    fx = _fixtures(target)
    session = _Session([fx.provisioned, fx.grant, None], fx.delivery)
    result, *_ = await _run(session, fx, target)
    assert result.disposition == "stale_progress" and session.calls[-1] == "rollback"

    fx2 = _fixtures(target)
    fx2.outbox.binding_cursor = "moved"
    session2 = _Session([fx2.provisioned, fx2.grant, fx2.outbox], fx2.delivery)
    result2, *_ = await _run(session2, fx2, target)
    assert result2.disposition == "stale_progress" and session2.calls[-1] == "rollback"

    fx3 = _fixtures(target)
    fx3.outbox.state = "done"
    session3 = _Session([fx3.provisioned, fx3.grant, fx3.outbox], fx3.delivery)
    result3, *_ = await _run(session3, fx3, target)
    assert result3.disposition == "stale_progress"


@pytest.mark.asyncio
async def test_prior_admission_is_replayed_without_new_writes() -> None:
    target = _target()
    fx = _fixtures(target)
    digest = connectors._github_target_digest(target)
    prior = connectors.GitHubHintEnqueueResult(
        workspace_id=WORKSPACE_ID,
        source_id=SOURCE_ID,
        hint_id=uuid4(),
        dirty_revision=3,
        disposition="coalesced",
    )
    admission = connectors._GitHubFanoutAdmission(
        workspace_id=WORKSPACE_ID,
        actor_user_id=7,
        membership_revision=3,
        workspace_configuration_revision=5,
        source_id=SOURCE_ID,
        target_sha256=digest,
        result=prior,
    )
    fx.outbox.fanout_page = _page(fx.binding, (admission,))
    session = _Session([fx.provisioned, fx.grant, fx.outbox], fx.delivery)
    result, *_ = await _run(session, fx, target)
    assert result == prior and session.calls == [*ROWS, "rollback"]


@pytest.mark.asyncio
async def test_missing_delivery_target_rolls_back_and_conflicts() -> None:
    target = _target()
    fx = _fixtures(target)
    fx.delivery.targets = []
    session = _Session([fx.provisioned, fx.grant, fx.outbox], fx.delivery)
    with pytest.raises(ConflictError, match="GitHub delivery target is no longer current"):
        await _run(session, fx, target)
    assert session.calls[-1] == "rollback"


@pytest.mark.asyncio
async def test_identity_and_principal_errors_are_raised_before_any_row_access() -> None:
    target = _target()
    fx = _fixtures(target)
    session = _Session([])
    bad_scope = InternalJobScope(
        workspace_id=WORKSPACE_ID,
        actor_user_id=7,
        membership_revision=3,
        source_id=uuid4(),
        source_generation=2,
    )
    with pytest.raises(ConflictError, match="GitHub fanout principal changed"):
        await connectors.enqueue_github_hint(
            session,
            binding=fx.binding,
            delivery_receipt_id=DELIVERY_ID,
            target=target,  # type: ignore[arg-type]
            expected_binding_cursor=None,
            scope=bad_scope,
            multi_workspace_enabled=False,
        )
    with pytest.raises(ValidationFailedError, match="GitHub fanout identity is invalid"):
        await connectors.enqueue_github_hint(
            session,
            binding=object(),
            delivery_receipt_id=DELIVERY_ID,
            target=target,  # type: ignore[arg-type]
            expected_binding_cursor=None,
            scope=SCOPE,
            multi_workspace_enabled=False,
        )
    assert session.calls == []


@pytest.mark.asyncio
async def test_configuration_revision_change_conflicts() -> None:
    target = _target()
    fx = _fixtures(target)
    stale = AccessFence(WORKSPACE_ID, 7, 3, 99)
    with (
        patch(f"{HINTS}._connector_access", AsyncMock(return_value=stale)),
        pytest.raises(ConflictError, match="GitHub fanout configuration changed"),
    ):
        await connectors.enqueue_github_hint(
            _Session([]),
            binding=fx.binding,
            delivery_receipt_id=DELIVERY_ID,
            target=target,  # type: ignore[arg-type]
            expected_binding_cursor=None,
            scope=SCOPE,
            multi_workspace_enabled=False,
        )


@pytest.mark.asyncio
async def test_visibility_lost_pauses_active_source_creates_hint_and_replays_change() -> None:
    target = _target(
        resource="issue", locator_kind="repository", locator="11", intent="visibility_lost"
    )
    fx = _fixtures(target, grant_state="revoked")
    capacity = SimpleNamespace(pending_count=3)
    session = _Session([fx.provisioned, fx.grant, fx.outbox, capacity], fx.delivery, many=[])
    result, prepare, pause, replay = await _run(session, fx, target)
    assert result.disposition == "ignored"
    prepare.assert_awaited_once()
    pause.assert_awaited_once()
    replay.assert_awaited_once()
    assert session.calls == [
        *ROWS,
        "scalars:github_source_hints",
        "scalar:github_webhook_capacity",
        "add",
    ]
    hint = session.added[0]
    assert (hint.state, hint.intent, hint.capacity_reserved, hint.source_generation) == (
        "visibility_unverified",
        "visibility_lost",
        False,
        3,
    )


@pytest.mark.asyncio
async def test_visibility_lost_rewrites_reserved_hint_and_releases_its_slot() -> None:
    target = _target(
        resource="issue", locator_kind="repository", locator="11", intent="visibility_lost"
    )
    fx = _fixtures(target, status="paused", grant_state="revoked")
    held = SimpleNamespace(
        resource="issue",
        locator_kind="repository",
        locator="11",
        dirty_revision=1,
        capacity_reserved=True,
        claim_token=uuid4(),
        claimed_revision=1,
        claim_expires_at=datetime.now(UTC),
    )
    capacity = SimpleNamespace(pending_count=2)
    session = _Session([fx.provisioned, fx.grant, fx.outbox, capacity], fx.delivery, many=[held])
    result, _prepare, _pause, replay = await _run(session, fx, target)
    assert result.disposition == "ignored" and session.calls[-1] == "commit"
    replay.assert_not_awaited()
    assert (held.state, held.dirty_revision, held.capacity_reserved, held.claim_token) == (
        "visibility_unverified",
        2,
        False,
        None,
    )
    assert capacity.pending_count == 1


@pytest.mark.asyncio
async def test_visibility_lost_errors_when_capacity_or_pause_disappear() -> None:
    target = _target(
        resource="issue", locator_kind="repository", locator="11", intent="visibility_lost"
    )
    fx = _fixtures(target, grant_state="revoked")
    with pytest.raises(ConflictError, match="GitHub visibility capacity disappeared"):
        await _run(_Session([fx.provisioned, fx.grant, fx.outbox, None], fx.delivery), fx, target)
    fx2 = _fixtures(target, grant_state="revoked")
    with pytest.raises(ConflictError, match="GitHub visibility Source changed during cleanup"):
        await _run(
            _Session(
                [fx2.provisioned, fx2.grant, fx2.outbox, SimpleNamespace(pending_count=1)],
                fx2.delivery,
            ),
            fx2,
            target,
            pause_source=AsyncMock(return_value=None),
        )
