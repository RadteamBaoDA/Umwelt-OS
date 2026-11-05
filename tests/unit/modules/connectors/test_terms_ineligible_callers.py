"""Regression: every caller of ``provider_terms.require_terms_eligible`` handles the real ``ConflictError``.

The gate raises ``ConflictError(TERMS_INELIGIBLE)`` (a ``DomainError``), not an ``HTTPException``; each caller
must map it exactly as it mapped the HTTP form. Both exception families are exercised.
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Self
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from core.errors import ConflictError
from modules.connectors import collection, provisioning_routes, routes, scheduler
from modules.connectors.provider_specs import TERMS_INELIGIBLE

FAMILIES = [
    pytest.param(lambda: ConflictError(TERMS_INELIGIBLE), id="domain_error"),
    pytest.param(
        lambda: HTTPException(status_code=409, detail=TERMS_INELIGIBLE), id="http_exception"
    ),
]


def _request() -> SimpleNamespace:
    """Minimal request exposing ``app.state.settings``."""
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(settings=SimpleNamespace(multi_workspace_enabled=False))
        )
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("make", FAMILIES)
async def test_manual_collect_maps_terms_to_terms_not_accepted(
    monkeypatch: pytest.MonkeyPatch, make
) -> None:
    session = MagicMock(rollback=AsyncMock())
    monkeypatch.setattr(routes, "min_interval_minutes", lambda provider: 0)
    monkeypatch.setattr(routes, "collection_cooldown_seconds", lambda *a: 0)
    monkeypatch.setattr(routes, "_owner_access", AsyncMock())
    monkeypatch.setattr(routes.scheduler, "request_collection", AsyncMock(side_effect=make()))
    with pytest.raises(HTTPException) as caught:
        await routes._queue_native_collection(
            session,
            _request(),
            uuid4(),
            SimpleNamespace(provider="binance"),
            1,  # type: ignore[arg-type]
            scope=SimpleNamespace(workspace_id=uuid4()),
            multi_workspace_enabled=False,  # type: ignore[arg-type]
            access_fence=SimpleNamespace(),  # type: ignore[arg-type]
        )
    assert (caught.value.status_code, caught.value.detail) == (409, "terms_not_accepted")


@pytest.mark.asyncio
async def test_manual_collect_reraises_other_domain_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(routes, "min_interval_minutes", lambda provider: 0)
    monkeypatch.setattr(routes, "collection_cooldown_seconds", lambda *a: 0)
    monkeypatch.setattr(routes, "_owner_access", AsyncMock())
    monkeypatch.setattr(
        routes.scheduler, "request_collection", AsyncMock(side_effect=ConflictError("other"))
    )
    with pytest.raises(ConflictError):
        await routes._queue_native_collection(
            MagicMock(rollback=AsyncMock()),
            _request(),
            uuid4(),
            SimpleNamespace(provider="binance"),
            1,  # type: ignore[arg-type]
            scope=SimpleNamespace(workspace_id=uuid4()),
            multi_workspace_enabled=False,  # type: ignore[arg-type]
            access_fence=SimpleNamespace(),  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("make", FAMILIES)
async def test_backend_transition_route_maps_terms_to_terms_not_accepted(
    monkeypatch: pytest.MonkeyPatch, make
) -> None:
    source = SimpleNamespace(id=uuid4(), status="active", type="rss", generation=1)
    monkeypatch.setattr(provisioning_routes, "_owner_access", AsyncMock())
    monkeypatch.setattr(provisioning_routes, "_source", AsyncMock(return_value=source))
    monkeypatch.setattr(provisioning_routes.registry, "SUPPORTED_TYPES", {"rss"})
    monkeypatch.setattr(
        provisioning_routes.provider_terms, "require_terms_eligible", AsyncMock(side_effect=make())
    )
    begin = AsyncMock()
    monkeypatch.setattr(provisioning_routes.provisioning, "begin_backend_transition_in_uow", begin)
    payload = SimpleNamespace(target_backend="n8n", expected_revision=1)
    with pytest.raises(HTTPException) as caught:
        await provisioning_routes.start_backend_transition(
            uuid4(),
            payload,
            MagicMock(),
            _request(),
            SimpleNamespace(workspace_id=uuid4()),  # type: ignore[arg-type]
        )
    assert (caught.value.status_code, caught.value.detail) == (409, "terms_not_accepted")
    begin.assert_not_awaited()


class _Ctx:
    """Async-context session double recording rollbacks."""

    def __init__(self, log: list[str]) -> None:
        """Bind the shared log."""
        self.log = log

    async def __aenter__(self) -> Self:
        """Enter the session scope."""
        return self

    async def __aexit__(self, *exc: object) -> bool:
        """Never swallow errors."""
        return False

    async def rollback(self) -> None:
        """Record the rollback."""
        self.log.append("rollback")


@pytest.mark.asyncio
@pytest.mark.parametrize("make", FAMILIES)
async def test_send_gate_translates_terms_to_terms_ineligible(
    monkeypatch: pytest.MonkeyPatch, make
) -> None:
    log: list[str] = []
    source = SimpleNamespace(
        id=uuid4(),
        provider="binance",
        type="api",
        generation=1,
        configuration={},
        workspace_id=uuid4(),
    )
    attempt = collection.Attempt(
        ref=SimpleNamespace(request_id=uuid4(), admission_token=uuid4()),  # type: ignore[arg-type]
        attempt=1,
        scope=SimpleNamespace(workspace_id=uuid4()),
        multi=True,  # type: ignore[arg-type]
        source=source,
        access_fence=None,
        source_fence=None,
        terms_revision=1,  # type: ignore[arg-type]
        connector_revision=2,
        authenticated=False,
    )
    monkeypatch.setattr(collection.scheduler, "renew_admission", AsyncMock(return_value=True))
    monkeypatch.setattr(collection.sources, "get_connector_source", AsyncMock(return_value=source))
    monkeypatch.setattr(
        collection.provider_terms, "require_terms_eligible", AsyncMock(side_effect=make())
    )
    gate = collection.SendGate(
        lambda: _Ctx(log), attempt, settings=None, deadline=1e18, quota_provider=None
    )  # type: ignore[arg-type]
    with pytest.raises(collection.TermsIneligible):
        await gate._authorize()
    assert log == ["rollback"]  # the session is released even on denial


@pytest.mark.asyncio
@pytest.mark.parametrize("make", FAMILIES)
async def test_scheduler_rolls_back_and_skips_terms_ineligible_source(
    monkeypatch: pytest.MonkeyPatch, make
) -> None:
    source_id, workspace_id = uuid4(), uuid4()
    log: list[str] = []

    class Session(_Ctx):
        """Scripted scheduler session: one picked source, owner and provisioning row present."""

        async def execute(self, stmt: object, params: object = None) -> object:
            """Return the picked ``(source_id, workspace_id)`` row for the selection query."""
            return SimpleNamespace(all=lambda: [(source_id, workspace_id)])

        async def commit(self) -> None:
            """Record the commit."""
            log.append("commit")

        async def scalar(self, stmt: object) -> object:
            """Return an owner membership."""
            return SimpleNamespace(user_id=1, revision=1)

        async def get(self, model: object, key: object) -> object:
            """Return a provisioning row."""
            return SimpleNamespace(source_generation=1)

    monkeypatch.setattr("modules.settings.public.module_is_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(scheduler, "_open_request", AsyncMock(side_effect=make()))
    created = await scheduler._create_due_requests(
        lambda: Session(log),
        datetime.now(UTC),
        False,  # type: ignore[arg-type]
    )
    assert created == 0
    assert (
        log.count("rollback") >= 2 and log.count("commit") == 1
    )  # no dispatch row after the denial
