"""Characterization tests for the native provider fetch dispatcher and its collector registry."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException

from core.errors import ConflictError, DomainError, UnavailableError, ValidationFailedError
from modules.connectors import native_fetch
from modules.connectors.native_fetch import (
    AcceptedCollection,
    BaseProviderCollector,
    CollectedPage,
    FetchContext,
    ProviderAdmissionBusy,
    ProviderFetchRequest,
    run_provider_fetch,
)
from modules.connectors.public import ProviderRateLimited

WS = uuid4()
SCOPE = SimpleNamespace(workspace_id=WS)


class Harness:
    """Patch every collaborator of ``run_provider_fetch`` and record the call order."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, provider: str = "fake_native") -> None:
        """Install spies; the admitted lease is a simple namespace."""
        self.calls: list[str] = []
        self.provider = provider
        self.lease = SimpleNamespace(
            token=uuid4(), source_generation=1, connector_revision=1, workspace_id=WS,
            configuration_revision=5, cursor_before="c0",
        )
        self.session = MagicMock()
        self.session.rollback = AsyncMock(side_effect=lambda: self.calls.append("rollback"))
        self.redis = MagicMock()
        self.request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
            settings=SimpleNamespace(multi_workspace_enabled=False), redis=self.redis,
        )))
        self.source = SimpleNamespace(id=uuid4(), provider=provider, generation=1, configuration={})
        self.payload = ProviderFetchRequest(
            source_generation=1, connector_revision=1, admission_request_id=uuid4(), admission_token=uuid4(),
        )
        self.access_fence = SimpleNamespace(configuration_revision=5)
        self.accepted: dict[str, Any] = {}

        def note(name: str, result: Any = None) -> AsyncMock:
            """Build an async spy that appends ``name`` to the call log."""
            async def fn(*a: Any, **kw: Any) -> Any:
                """Record the call and return the canned result."""
                self.calls.append(name)
                self.last_kwargs = kw
                return result
            return AsyncMock(side_effect=fn)

        monkeypatch.setattr(native_fetch, "is_native_provider", lambda p: True)
        monkeypatch.setattr(native_fetch.registry, "validate", lambda s: None)
        monkeypatch.setattr(native_fetch.provisioning, "require_collection_fence", note("fence", True))
        monkeypatch.setattr(native_fetch, "provider_cooldown", note("cooldown", None))
        monkeypatch.setattr(native_fetch.ingestion, "acquire_connector_collection", note("acquire", self.lease))
        monkeypatch.setattr(native_fetch.sources, "get_source_fence", note(
            "source_fence", SimpleNamespace(generation=1, workspace_id=WS)))
        monkeypatch.setattr(native_fetch, "release_failed_collection", note("release"))
        monkeypatch.setattr(native_fetch, "settle_request_exit", note("settle"))
        monkeypatch.setattr(native_fetch, "extend_provider_cooldown", note("extend", datetime.now(UTC) + timedelta(minutes=1)))
        monkeypatch.setattr(native_fetch.ingestion, "release_connector_collection", note("release_lease"))

        async def accept(session: Any, batch: Any, **kw: Any) -> Any:
            """Capture the accepted batch and return a queued receipt."""
            self.calls.append("accept")
            self.accepted = {"batch": batch, **kw}
            return SimpleNamespace(
                workspace_id=WS, status="queued", batch_id=uuid4(), run_id=uuid4(),
                received_update_count=0, record_count=len(batch.records), coverage=batch.coverage,
                next_eligible_at=None,
            )

        monkeypatch.setattr(native_fetch.ingestion, "accept_native_collection", accept)

    async def run(self) -> Any:
        """Invoke the dispatcher with the harness collaborators."""
        return await run_provider_fetch(
            self.session, self.request, self.source, self.payload,  # type: ignore[arg-type]
            collector_token="tok", request_ref=SimpleNamespace(), scope=SCOPE,  # type: ignore[arg-type]
            access_fence=self.access_fence,  # type: ignore[arg-type]
        )


class _Page(BaseProviderCollector):
    """Collector returning a canned page and recording hook order."""

    def __init__(self, harness: Harness, page: CollectedPage | None = None, error: BaseException | None = None) -> None:
        """Bind the canned outcome."""
        self.h = harness
        self.page = page
        self.error = error

    async def prepare(self, ctx: FetchContext) -> None:
        """Record that preparation ran before collection."""
        self.h.calls.append("prepare")

    async def collect(self, ctx: FetchContext) -> CollectedPage | AcceptedCollection:
        """Record collection, then raise or return the canned page."""
        self.h.calls.append("collect")
        if self.error is not None:
            raise self.error
        assert self.page is not None
        return self.page


def _page(**kw: Any) -> CollectedPage:
    """Build a one-record page with defaults."""
    return CollectedPage(records=[], coverage="returned_snapshot", collected_at=datetime.now(UTC), **kw)


def _use(monkeypatch: pytest.MonkeyPatch, harness: Harness, collector: BaseProviderCollector) -> None:
    """Register ``collector`` for the harness provider and restore the registry afterwards."""
    monkeypatch.setitem(native_fetch._COLLECTORS, harness.provider, collector)


@pytest.mark.asyncio
async def test_registered_provider_collects_and_accepts_without_dispatcher_edit(monkeypatch: pytest.MonkeyPatch) -> None:
    h = Harness(monkeypatch)
    order: list[str] = []

    async def pre_accept() -> None:
        """Record the pre-accept hook."""
        order.append("pre_accept")
        h.calls.append("pre_accept")

    _use(monkeypatch, h, _Page(h, _page(pre_accept=pre_accept, cursor_after="c1", has_cursor_after=True)))
    read = await h.run()
    assert read.status == "queued"
    assert h.calls == [
        "fence", "rollback", "cooldown", "acquire", "source_fence", "rollback", "prepare", "collect",
        "pre_accept", "accept",
    ]
    batch = h.accepted["batch"]
    assert batch.cursor_after == "c1" and batch.cursor_before == "c0"
    assert h.accepted["expected_world_credential_operation_id"] is None


@pytest.mark.asyncio
async def test_cursor_defaults_to_lease_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    h = Harness(monkeypatch)
    _use(monkeypatch, h, _Page(h, _page()))
    await h.run()
    assert h.accepted["batch"].cursor_after == "c0"


@pytest.mark.asyncio
async def test_provider_retry_deadline_page_releases_lease_without_receipt(monkeypatch: pytest.MonkeyPatch) -> None:
    h = Harness(monkeypatch)
    hook = AsyncMock()
    _use(monkeypatch, h, _Page(h, _page(next_eligible_at=datetime.now(UTC) + timedelta(minutes=5), pre_accept=hook)))
    read = await h.run()
    assert read.status == "rate_limited" and read.coverage == "returned_snapshot"
    assert h.calls[-3:] == ["extend", "release_lease", "settle"]
    assert "accept" not in h.calls
    hook.assert_not_awaited()  # pre_accept must not run for a rate-limited page


@pytest.mark.asyncio
async def test_post_collect_hook_runs_before_rate_limit_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    h = Harness(monkeypatch)

    async def post() -> None:
        """Fail like the Alpha credential capture check."""
        raise ConflictError("Alpha credential capture changed during collection")

    _use(monkeypatch, h, _Page(h, _page(post_collect=post, next_eligible_at=datetime.now(UTC) + timedelta(minutes=5))))
    with pytest.raises(DomainError) as exc:
        await h.run()
    assert exc.value.status_code == 409
    assert "extend" not in h.calls
    assert h.calls[-2:] == ["release", "settle"]


@pytest.mark.asyncio
async def test_cooldown_before_lease_returns_rate_limited_without_acquiring(monkeypatch: pytest.MonkeyPatch) -> None:
    h = Harness(monkeypatch, provider="telegram")
    monkeypatch.setattr(native_fetch, "provider_cooldown", AsyncMock(return_value=datetime.now(UTC) + timedelta(minutes=2)))
    read = await h.run()
    assert read.status == "rate_limited" and read.coverage == "pending_updates_only"
    assert "acquire" not in h.calls and h.calls[-1] == "settle"


@pytest.mark.asyncio
async def test_stale_fence_is_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    h = Harness(monkeypatch)
    monkeypatch.setattr(native_fetch.provisioning, "require_collection_fence", AsyncMock(return_value=False))
    with pytest.raises(ConflictError, match="Connector collection fence is stale"):
        await h.run()


@pytest.mark.asyncio
async def test_changed_reservation_rolls_back_and_conflicts(monkeypatch: pytest.MonkeyPatch) -> None:
    h = Harness(monkeypatch)
    h.lease.configuration_revision = 99
    with pytest.raises(ConflictError, match="Original collection reservation changed"):
        await h.run()
    assert h.calls[-1] == "rollback"


@pytest.mark.asyncio
async def test_admission_timeout_is_503(monkeypatch: pytest.MonkeyPatch) -> None:
    h = Harness(monkeypatch)
    monkeypatch.setattr(native_fetch.ingestion, "acquire_connector_collection", AsyncMock(side_effect=TimeoutError))
    with pytest.raises(UnavailableError, match="admission exceeded its deadline"):
        await h.run()


@pytest.mark.asyncio
async def test_github_deadline_starts_before_fencing(monkeypatch: pytest.MonkeyPatch) -> None:
    h = Harness(monkeypatch, provider="github")
    seen: dict[str, Any] = {}

    async def fence(*a: Any, **kw: Any) -> bool:
        """Capture the remaining GitHub segment budget when fencing starts."""
        seen["t"] = asyncio.get_running_loop().time()
        return True

    monkeypatch.setattr(native_fetch.provisioning, "require_collection_fence", fence)
    collected: dict[str, Any] = {}

    class C(_Page):
        """Capture the context deadline."""

        async def collect(self, ctx: FetchContext) -> CollectedPage:
            """Record the segment deadline relative to fencing start."""
            collected["deadline"] = ctx.segment_deadline
            return _page()

    _use(monkeypatch, h, C(h))
    await h.run()
    assert 59 < collected["deadline"] - seen["t"] <= 60.01


@pytest.mark.asyncio
async def test_non_native_source_is_rejected_before_admission(monkeypatch: pytest.MonkeyPatch) -> None:
    h = Harness(monkeypatch, provider="x")
    monkeypatch.setattr(native_fetch, "is_native_provider", lambda p: False)
    with pytest.raises(ConflictError, match="Native provider is not configured") as info:
        await h.run()
    assert info.value.status_code == 409 and "acquire" not in h.calls


@pytest.mark.asyncio
async def test_scheduler_only_and_invalid_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    h = Harness(monkeypatch)
    monkeypatch.setattr(native_fetch, "GATED_EXECUTOR_PROVIDERS", frozenset({"fake_native"}))
    with pytest.raises(ConflictError, match="scheduler only"):
        await h.run()
    monkeypatch.setattr(native_fetch, "GATED_EXECUTOR_PROVIDERS", frozenset())

    def bad(_s: Any) -> None:
        """Reject the stored configuration."""
        raise ValueError("bad")

    monkeypatch.setattr(native_fetch.registry, "validate", bad)
    with pytest.raises(ValidationFailedError, match="configuration is invalid"):
        await h.run()


@pytest.mark.asyncio
async def test_collector_rate_limit_extends_cooldown_then_releases(monkeypatch: pytest.MonkeyPatch) -> None:
    h = Harness(monkeypatch, provider="telegram")
    _use(monkeypatch, h, _Page(h, error=ProviderRateLimited(datetime.now(UTC) + timedelta(minutes=1))))
    read = await h.run()
    assert read.status == "rate_limited" and read.coverage == "pending_updates_only"
    assert h.calls[-3:] == ["extend", "release", "settle"]


@pytest.mark.asyncio
async def test_admission_busy_returns_one_second_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    h = Harness(monkeypatch)
    _use(monkeypatch, h, _Page(h, error=ProviderAdmissionBusy("busy")))
    before = datetime.now(UTC)
    read = await h.run()
    assert read.status == "rate_limited" and read.coverage == "returned_snapshot"
    assert read.next_eligible_at is not None and read.next_eligible_at >= before + timedelta(seconds=1)
    assert h.calls[-2:] == ["release", "settle"]


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [HTTPException(409, "x"), ConflictError("x")])
async def test_http_errors_release_settle_and_reraise_same_exception(monkeypatch: pytest.MonkeyPatch, error: BaseException) -> None:
    h = Harness(monkeypatch)
    _use(monkeypatch, h, _Page(h, error=error))
    with pytest.raises(type(error)) as info:
        await h.run()
    assert info.value is error
    assert h.calls[-2:] == ["release", "settle"]


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [TimeoutError(), httpx.ConnectError("x"), ValueError("x")])
async def test_transport_errors_become_503(monkeypatch: pytest.MonkeyPatch, error: BaseException) -> None:
    h = Harness(monkeypatch)
    _use(monkeypatch, h, _Page(h, error=error))
    with pytest.raises(UnavailableError, match="Provider collection failed") as info:
        await h.run()
    assert info.value.__cause__ is error
    assert h.calls[-2:] == ["release", "settle"]


@pytest.mark.asyncio
async def test_cancellation_releases_lease_and_reraises(monkeypatch: pytest.MonkeyPatch) -> None:
    h = Harness(monkeypatch)
    _use(monkeypatch, h, _Page(h, error=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        await h.run()
    assert h.calls[-2:] == ["release", "settle"]


@pytest.mark.asyncio
async def test_prepare_failure_releases_lease_and_skips_collect(monkeypatch: pytest.MonkeyPatch) -> None:
    h = Harness(monkeypatch)

    class Boom(_Page):
        """Fail during the pre-window hook."""

        async def prepare(self, ctx: FetchContext) -> None:
            """Raise before the collection window opens."""
            raise RuntimeError("claim failed")

    _use(monkeypatch, h, Boom(h))
    with pytest.raises(RuntimeError):
        await h.run()
    assert h.calls[-1] == "release" and "collect" not in h.calls


@pytest.mark.asyncio
async def test_github_permit_released_in_finally(monkeypatch: pytest.MonkeyPatch) -> None:
    h = Harness(monkeypatch, provider="github")
    release = AsyncMock()
    monkeypatch.setattr(native_fetch, "release_github_network_permit", release)

    class C(_Page):
        """Take a permit, then fail."""

        async def collect(self, ctx: FetchContext) -> CollectedPage:
            """Hold a permit token and raise."""
            ctx.github_permit_token = "permit"
            raise ConflictError("boom")

    _use(monkeypatch, h, C(h))
    with pytest.raises(ConflictError):
        await h.run()
    release.assert_awaited_once_with(h.request, "permit")


@pytest.mark.asyncio
async def test_accepted_collection_outcomes(monkeypatch: pytest.MonkeyPatch) -> None:
    h = Harness(monkeypatch, provider="telegram")
    receipt = SimpleNamespace(
        workspace_id=WS, status="succeeded", batch_id=None, run_id=None, received_update_count=0,
        record_count=0, coverage="pending_updates_only", next_eligible_at=None,
    )

    class T(BaseProviderCollector):
        """Return an already accepted replay-only receipt."""

        async def collect(self, ctx: FetchContext) -> AcceptedCollection:
            """Return a receipt without a batch."""
            return AcceptedCollection(receipt=receipt)  # type: ignore[arg-type]

    _use(monkeypatch, h, T())
    read = await h.run()
    assert read.status == "succeeded" and read.batch_id is None
    assert h.calls[-1] == "settle"  # replay-only: nothing accepted, so the dispatcher settles no_changes
    assert h.last_kwargs == {"outcome": "no_changes"}


def test_default_collector_for_unregistered_provider() -> None:
    from modules.connectors.native_collectors import GithubReleasesCollector

    assert isinstance(native_fetch.resolve_collector("something_new"), GithubReleasesCollector)
    assert isinstance(native_fetch.resolve_collector(None), GithubReleasesCollector)
    from modules.connectors import native_collectors as nc

    expected = {
        "youtube": nc.FeedCollector, "arxiv": nc.FeedCollector, "huggingface": nc.HuggingFaceCollector,
        "github": nc.GithubCollector, "alpha_vantage": nc.WorldDataCollector,
        "open_meteo": nc.WorldDataCollector, "telegram": nc.TelegramCollector,
    }
    for provider, cls in expected.items():
        assert type(native_fetch.resolve_collector(provider)) is cls


def test_error_classification_handles_both_families() -> None:
    assert native_fetch.request_failure(ConflictError("terms_not_accepted")) == ("terms_not_accepted", False)
    assert native_fetch.request_failure(ValidationFailedError("bad")) == ("schema_changed", False)
    assert native_fetch.request_failure(ConflictError(native_fetch.GITHUB_RECONNECT_DETAIL)) == ("invalid_credential", False)
    assert native_fetch.request_failure(UnavailableError("x")) == ("provider_collection_failed", True)
    assert native_fetch.collection_error_code(ConflictError(native_fetch.GITHUB_RECONNECT_DETAIL)) == "provider_unauthorized"
