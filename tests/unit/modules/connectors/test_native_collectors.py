"""Characterization tests for the real native collectors (Telegram, GitHub, world data).

Collaborators are replaced by spies so the tests pin lock order, lease publication, release
semantics, error mapping and the pre/post-accept hooks without a database or network.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from core.errors import ConflictError, ValidationFailedError
from modules.connectors import native_collectors as nc
from modules.connectors.native_fetch import (
    GITHUB_RECONNECT_DETAIL,
    AcceptedCollection,
    ProviderAdmissionBusy,
)
from modules.connectors.public import ProviderRateLimited

WS = uuid4()


def _lease(**kw: Any) -> SimpleNamespace:
    """Build a lease double with the fields the collectors read."""
    base = {
        "token": uuid4(), "source_generation": 1, "connector_revision": 1, "workspace_id": WS, "actor_user_id": 7,
        "membership_revision": 2, "configuration_revision": 5, "cursor_before": "c0",
    }
    return SimpleNamespace(**{**base, **kw})


def _ctx(log: list[str], lease: Any | None = None, provider: str = "telegram") -> SimpleNamespace:
    """Build a FetchContext double whose ``send_fence`` records the lease it was given."""
    lease = lease or _lease()
    session = MagicMock()

    async def rollback() -> None:
        """Record the rollback in the shared call log."""
        log.append("rollback")

    session.rollback = AsyncMock(side_effect=rollback)
    ctx = SimpleNamespace(
        session=session, lease=lease, active_lease=[lease], scope=SimpleNamespace(workspace_id=WS),
        multi_workspace_enabled=False, access_fence=SimpleNamespace(configuration_revision=5),
        source_fence=SimpleNamespace(), collector_token="tok", request_ref=SimpleNamespace(),
        source=SimpleNamespace(id=uuid4(), provider=provider, generation=1,
                               configuration={"telegram_chat_ids": ["1"]}),
        payload=SimpleNamespace(source_generation=1, connector_revision=1),
        request=SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
            settings=SimpleNamespace(connector_credential_encryption_key=SimpleNamespace(get_secret_value=lambda: "k")),
            redis=MagicMock(), session_factory=MagicMock()))),
        github_permit_token=None, prepared=None, fences=[],
    )

    async def send_fence(*, lease: Any = None, **extra: Any) -> None:
        """Record which lease and proofs each physical send was fenced with."""
        ctx.fences.append((lease if lease is not None else ctx.lease, extra))
        log.append("send_fence")

    ctx.send_fence = send_fence
    return ctx


# --------------------------------------------------------------------------- Telegram

class _Tg:
    """Patch every Telegram collaborator and script the fetched pages."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, pages: list[Any], ctx: SimpleNamespace, log: list[str]) -> None:
        """Install spies; ``pages`` entries may be exceptions (raised) or page doubles (returned)."""
        self.pages, self.ctx, self.log = list(pages), ctx, log
        self.offsets: list[int | None] = []
        self.fence_leases_in_fetch: list[Any] = []
        self.released: list[tuple[Any, Any]] = []
        self.new_leases: list[Any] = []
        self.cursor = SimpleNamespace(last_update_id=41)
        self.classifications: list[Any] = []
        snapshot = SimpleNamespace(state="ready", encrypted_token="x", verified_bot_id="9", operation_id=uuid4())
        monkeypatch.setattr(nc.provisioning, "lock_connector", AsyncMock(side_effect=lambda *a, **k: log.append("lock")))
        monkeypatch.setattr(nc, "get_native_credential_snapshot", AsyncMock(return_value=snapshot))
        monkeypatch.setattr("modules.connectors.credentials.decrypt_native_token", lambda key, snap: "bot-token")
        monkeypatch.setattr(nc.ingestion, "read_telegram_collection_state", AsyncMock(side_effect=lambda *a, **k: self.cursor))
        monkeypatch.setattr(nc.ingestion, "release_connector_collection", AsyncMock(side_effect=self._release))
        monkeypatch.setattr(nc.ingestion, "acquire_connector_collection", AsyncMock(side_effect=self._acquire))
        monkeypatch.setattr(nc.ingestion, "accept_native_collection", AsyncMock(return_value="receipt"))
        monkeypatch.setattr(nc, "classify_telegram_probe", self._classify)
        monkeypatch.setattr("modules.connectors.providers.telegram.fetch_telegram_updates", self._fetch)
        monkeypatch.setattr("modules.connectors.providers.telegram.map_telegram_update", lambda *a, **k: None)
        monkeypatch.setattr(nc, "extend_provider_cooldown", AsyncMock(side_effect=lambda r, p, at: at))

    async def _release(self, session: Any, lease: Any, **kw: Any) -> None:
        """Record which lease was released and with which error code."""
        self.released.append((lease, kw.get("error_code")))

    async def _acquire(self, *a: Any, **k: Any) -> Any:
        """Hand out the next scripted replacement lease."""
        lease = self.new_leases.pop(0)
        return lease

    def _classify(self, cursor: Any, deliveries: Any, **kw: Any) -> Any:
        """Return the next scripted classification."""
        return self.classifications.pop(0)

    async def _fetch(self, token: str, *, offset: int | None, remaining_bytes: int, before_request: Any) -> Any:
        """Record the offset and prove ``before_request`` late-binds the current lease."""
        self.offsets.append(offset)
        await before_request()
        self.fence_leases_in_fetch.append(self.ctx.fences[-1][0])
        item = self.pages.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _page(n_deliveries: int = 1, transport_bytes: int = 10, first_id: int = 1) -> SimpleNamespace:
    """Build a Telegram page double with sequential update ids."""
    deliveries = tuple(SimpleNamespace(update_id=first_id + i, update={}) for i in range(n_deliveries))
    return SimpleNamespace(deliveries=deliveries, transport_bytes=transport_bytes, collected_at=datetime.now(UTC))


def _cls(replay: tuple[int, ...] = (), conflict: str | None = None) -> SimpleNamespace:
    """Build a probe classification double."""
    return SimpleNamespace(conflict_code=conflict, replay_update_ids=replay, delivery_proofs=[], cursor_after=None)


@pytest.mark.asyncio
async def test_telegram_all_replay_page_releases_reacquires_and_rebinds_lease(monkeypatch: pytest.MonkeyPatch) -> None:
    log: list[str] = []
    ctx = _ctx(log)
    first = ctx.lease
    tg = _Tg(monkeypatch, [_page(1, first_id=1), _page(1, first_id=2)], ctx, log)
    second = _lease()
    tg.new_leases = [second]
    second_cls = _cls()
    second_cls.delivery_proofs = [SimpleNamespace(update_id=2)]
    tg.classifications = [_cls(replay=(1,)), second_cls]
    monkeypatch.setattr(nc, "NativeCollectionBatch", lambda **kw: SimpleNamespace(**kw))
    result = await nc.TelegramCollector().collect(ctx)
    assert result.receipt == "receipt"
    assert tg.released == [(first, None)]  # a replay page settles without an error code
    assert tg.offsets == [None, 42]  # last_update_id + 1 on the continuation only
    assert tg.fence_leases_in_fetch == [first, second]  # before_request late-binds the rebound lease
    assert ctx.active_lease[0] is second
    accept_kwargs = nc.ingestion.accept_native_collection.await_args.kwargs
    assert accept_kwargs["lease"] is second


@pytest.mark.asyncio
async def test_telegram_publishes_new_lease_only_after_revision_check(monkeypatch: pytest.MonkeyPatch) -> None:
    log: list[str] = []
    ctx = _ctx(log)
    first = ctx.lease
    tg = _Tg(monkeypatch, [_page(1)], ctx, log)
    tg.new_leases = [_lease(configuration_revision=99)]  # access changed since admission
    tg.classifications = [_cls(replay=(1,))]
    with pytest.raises(ConflictError, match="Original Telegram access changed"):
        await nc.TelegramCollector().collect(ctx)
    assert ctx.active_lease[0] is first  # the unverified lease was never published


@pytest.mark.asyncio
@pytest.mark.parametrize("cursor", [None, SimpleNamespace(last_update_id=2**63 - 1)])
async def test_telegram_replay_without_usable_cursor_returns_empty_succeeded_receipt(
    monkeypatch: pytest.MonkeyPatch, cursor: Any,
) -> None:
    log: list[str] = []
    ctx = _ctx(log)
    tg = _Tg(monkeypatch, [_page(1)], ctx, log)
    tg.cursor = cursor
    tg.classifications = [_cls(replay=(1,))]
    result = await nc.TelegramCollector().collect(ctx)
    assert result.receipt.status == "succeeded" and result.receipt.coverage == "pending_updates_only"
    assert result.receipt.record_count == 0 and result.receipt.cursor_after == "c0"
    nc.ingestion.acquire_connector_collection.assert_not_awaited()


@pytest.mark.asyncio
async def test_telegram_fifth_replay_page_returns_empty_receipt(monkeypatch: pytest.MonkeyPatch) -> None:
    log: list[str] = []
    ctx = _ctx(log)
    tg = _Tg(monkeypatch, [_page(1, first_id=i + 1) for i in range(5)], ctx, log)
    tg.new_leases = [_lease() for _ in range(4)]
    tg.classifications = [_cls(replay=(i + 1,)) for i in range(5)]
    result = await nc.TelegramCollector().collect(ctx)
    assert result.receipt.status == "succeeded" and result.receipt.received_update_count == 0
    assert nc.ingestion.acquire_connector_collection.await_count == 4  # page_number 4 never re-acquires


@pytest.mark.asyncio
async def test_telegram_rate_limit_releases_current_lease_without_receipt(monkeypatch: pytest.MonkeyPatch) -> None:
    log: list[str] = []
    ctx = _ctx(log)
    deadline = datetime.now(UTC) + timedelta(minutes=5)
    tg = _Tg(monkeypatch, [ProviderRateLimited(deadline)], ctx, log)
    result = await nc.TelegramCollector().collect(ctx)
    assert isinstance(result, AcceptedCollection) and result.receipt is None
    assert result.rate_limited.status == "rate_limited"
    assert result.rate_limited.coverage == "pending_updates_only"
    assert result.rate_limited.next_eligible_at == deadline
    assert [(lease, code) for lease, code in tg.released] == [(ctx.lease, "provider_rate_limited")]
    nc.ingestion.accept_native_collection.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(("page", "cls", "error", "status"), [
    (_page(1, transport_bytes=25 * 1024 * 1024 + 1), _cls(), ValidationFailedError, 422),
    (_page(501), _cls(), ValidationFailedError, 422),
    (_page(1), _cls(conflict="stream"), ConflictError, 409),
])
async def test_telegram_ceilings_and_conflict_status_codes(
    monkeypatch: pytest.MonkeyPatch, page: Any, cls: Any, error: type[Exception], status: int,
) -> None:
    log: list[str] = []
    ctx = _ctx(log)
    tg = _Tg(monkeypatch, [page], ctx, log)
    tg.classifications = [cls]
    with pytest.raises(error) as caught:
        await nc.TelegramCollector().collect(ctx)
    assert caught.value.status_code == status


# --------------------------------------------------------------------------- GitHub

def _grant(**kw: Any) -> SimpleNamespace:
    """Build a ready GitHub grant double that matches the default lease/source."""
    base = {"state": "ready", "encrypted_tokens": b"x", "source_generation": 1, "configuration_revision": 1,
            "expires_at": datetime.now(UTC) + timedelta(hours=1), "operation_id": uuid4(), "token_revision": 3}
    return SimpleNamespace(**{**base, **kw})


def _github(monkeypatch: pytest.MonkeyPatch, ctx: SimpleNamespace, log: list[str], *, fence: Any = "FENCE",
            grant: Any = None, permit: str | None = "permit") -> SimpleNamespace:
    """Patch the GitHub collaborators and return handles used by the assertions."""
    grant_obj = grant
    monkeypatch.setattr(nc.provisioning, "lock_connector", AsyncMock(side_effect=lambda *a, **k: log.append("lock_connector")))
    monkeypatch.setattr(nc.connectors_public, "lock_github_binding_fence_in_uow",
                        AsyncMock(side_effect=lambda *a, **k: (log.append("lock_fence"), fence)[1]))

    async def scalar(stmt: Any) -> Any:
        """Record the grant select and return the scripted grant."""
        log.append("grant_select")
        return grant_obj

    ctx.session.scalar = scalar
    monkeypatch.setattr(nc.github_oauth, "_open_token_cipher",
                        lambda *a: log.append("decrypt") or {"access_token": "gh-token"})
    monkeypatch.setattr(nc, "project_github_source_config", lambda cfg: "CONFIG")
    monkeypatch.setattr(nc, "provider_cooldown", AsyncMock(return_value=None))
    monkeypatch.setattr(nc, "acquire_github_network_permit", AsyncMock(return_value=permit))
    handles = SimpleNamespace(before=None)

    async def segment(config: Any, token: str, *, before_request: Any, **kw: Any) -> str:
        """Drive the per-send hook once, as the adapter does before each request."""
        handles.before = before_request
        await before_request()
        return "PROOF"

    monkeypatch.setattr(nc, "collect_github_segment", segment)
    monkeypatch.setattr(nc, "validate_github_segment", lambda *a, **k: SimpleNamespace(
        records=[], coverage="returned_snapshot", cursor_after="c9"))
    return handles


@pytest.mark.asyncio
async def test_github_collect_locks_in_order_rolls_back_before_io_and_stores_permit(monkeypatch: pytest.MonkeyPatch) -> None:
    log: list[str] = []
    ctx = _ctx(log, provider="github")
    grant = _grant()
    _github(monkeypatch, ctx, log, grant=grant)
    page = await nc.GithubCollector().collect(ctx)
    assert log[:5] == ["lock_connector", "lock_fence", "grant_select", "decrypt", "rollback"]
    assert log.index("rollback") < log.index("send_fence")  # no lock is held across provider I/O
    assert ctx.github_permit_token == "permit"
    assert page.github_segment == "PROOF" and page.cursor_after == "c9" and page.has_cursor_after
    _, extra = ctx.fences[0]
    assert extra == {"github_fence": "FENCE", "github_binding": (grant.operation_id, 3)}


@pytest.mark.asyncio
async def test_github_pre_accept_reproves_fence_and_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    log: list[str] = []
    ctx = _ctx(log, provider="github")
    grant = _grant()
    _github(monkeypatch, ctx, log, grant=grant)
    page = await nc.GithubCollector().collect(ctx)
    before = len(ctx.fences)
    await page.pre_accept()
    assert len(ctx.fences) == before + 1
    assert ctx.fences[-1][1] == {"github_fence": "FENCE", "github_binding": (grant.operation_id, 3)}


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["no_fence", "no_grant", "not_ready", "expired", "wrong_revision"])
async def test_github_missing_or_mismatched_proof_requires_reconnect(monkeypatch: pytest.MonkeyPatch, scenario: str) -> None:
    log: list[str] = []
    ctx = _ctx(log, provider="github")
    grants = {
        "no_fence": _grant(), "no_grant": None, "not_ready": _grant(state="revoked"),
        "expired": _grant(expires_at=datetime.now(UTC) - timedelta(seconds=1)),
        "wrong_revision": _grant(configuration_revision=2),
    }
    _github(monkeypatch, ctx, log, fence=None if scenario == "no_fence" else "FENCE", grant=grants[scenario])
    with pytest.raises(ConflictError) as caught:
        await nc.GithubCollector().collect(ctx)
    assert caught.value.status_code == 409 and caught.value.detail == GITHUB_RECONNECT_DETAIL
    assert "send_fence" not in log


@pytest.mark.asyncio
async def test_github_busy_permit_raises_admission_busy(monkeypatch: pytest.MonkeyPatch) -> None:
    log: list[str] = []
    ctx = _ctx(log, provider="github")
    _github(monkeypatch, ctx, log, grant=_grant(), permit=None)
    with pytest.raises(ProviderAdmissionBusy):
        await nc.GithubCollector().collect(ctx)
    assert ctx.github_permit_token is None


@pytest.mark.asyncio
async def test_github_cooldown_blocks_before_fence(monkeypatch: pytest.MonkeyPatch) -> None:
    log: list[str] = []
    ctx = _ctx(log, provider="github")
    _github(monkeypatch, ctx, log, grant=_grant())
    until = datetime.now(UTC) + timedelta(minutes=3)
    monkeypatch.setattr(nc, "provider_cooldown", AsyncMock(return_value=until))
    with pytest.raises(ProviderRateLimited):
        await nc.GithubCollector().collect(ctx)
    assert ctx.fences == []


# --------------------------------------------------------------------------- World data

def _world(monkeypatch: pytest.MonkeyPatch, ops: list[Any], page_op: Any) -> None:
    """Patch ``collect_world_data`` to announce ``ops`` through ``before_request`` and return a page."""
    async def fake(source: Any, **kw: Any) -> Any:
        """Call the per-GET hook once per scripted operation id, then return the page."""
        for op in ops:
            await kw["before_request"](op)
        return SimpleNamespace(records=[], coverage="returned_snapshot", next_eligible_at=None,
                               credential_operation_id=page_op)

    monkeypatch.setattr("modules.connectors.providers.world_data.collect_world_data", fake)


@pytest.mark.asyncio
async def test_alpha_capture_matches_sets_operation_id_and_post_collect_fences(monkeypatch: pytest.MonkeyPatch) -> None:
    log: list[str] = []
    ctx = _ctx(log, provider="alpha_vantage")
    op = uuid4()
    _world(monkeypatch, [op, op], op)
    page = await nc.WorldDataCollector().collect(ctx)
    assert page.world_operation_id == op and page.post_collect is not None
    before = len(ctx.fences)
    await page.post_collect()
    assert len(ctx.fences) == before + 1 and ctx.fences[-1][1] == {"world_operation_id": op}


@pytest.mark.asyncio
async def test_alpha_page_operation_mismatch_is_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _ctx([], provider="alpha_vantage")
    op = uuid4()
    _world(monkeypatch, [op], uuid4())
    page = await nc.WorldDataCollector().collect(ctx)
    with pytest.raises(ConflictError, match="changed during collection"):
        await page.post_collect()


@pytest.mark.asyncio
async def test_alpha_operation_change_between_gets_and_missing_capture_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _ctx([], provider="alpha_vantage")
    _world(monkeypatch, [uuid4(), uuid4()], None)
    with pytest.raises(ConflictError, match="operation changed"):
        await nc.WorldDataCollector().collect(ctx)
    _world(monkeypatch, [None], None)
    with pytest.raises(ConflictError, match="capture is missing"):
        await nc.WorldDataCollector().collect(_ctx([], provider="alpha_vantage"))


@pytest.mark.asyncio
async def test_open_meteo_has_no_world_operation_or_post_collect(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _ctx([], provider="open_meteo")
    _world(monkeypatch, [None], None)
    page = await nc.WorldDataCollector().collect(ctx)
    assert page.world_operation_id is None and page.post_collect is None
    assert ctx.fences[0][1] == {"world_operation_id": None}
    _world(monkeypatch, [uuid4()], None)
    with pytest.raises(ConflictError, match="Unexpected world credential capture"):
        await nc.WorldDataCollector().collect(_ctx([], provider="open_meteo"))
