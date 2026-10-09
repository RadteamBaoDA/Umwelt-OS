"""P1 terms gate: deny-by-default eligibility, operator-only review, scheduler capture (no DB, no network)."""

from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

from modules.connectors import provider_terms, scheduler
from modules.connectors.models import ConnectorCollectionRequest, ConnectorProviderTerms

WORKSPACE_ID, SOURCE_ID = uuid4(), uuid4()


def _source(provider: str | None) -> Any:
    return SimpleNamespace(id=SOURCE_ID, workspace_id=WORKSPACE_ID, provider=provider, generation=1, status="active")


def _row(**over: Any) -> ConnectorProviderTerms:
    base = {
        "source_id": SOURCE_ID, "workspace_id": WORKSPACE_ID, "provider_id": "world_bank", "terms_revision": 3,
        "terms_url": "https://x", "terms_version": "v1", "checked_on": date(2026, 10, 7),
        "owner_acknowledged_at": datetime.now(UTC), "owner_actor_user_id": 1, "declared_use": "unknown",
        "operator_review_state": "pending",
    }
    return ConnectorProviderTerms(**(base | over))


def _session(row: Any) -> MagicMock:
    return MagicMock(scalar=AsyncMock(return_value=row), flush=AsyncMock(), commit=AsyncMock(), rollback=AsyncMock())


async def test_non_catalog_provider_has_no_terms_gate_and_no_query() -> None:
    session = _session(None)
    assert await provider_terms.require_terms_eligible(session, _source("github")) is None
    assert await provider_terms.require_terms_eligible(session, _source(None)) is None
    session.scalar.assert_not_called()


async def test_catalog_provider_without_acknowledgement_is_denied() -> None:
    with pytest.raises(HTTPException) as caught:
        await provider_terms.require_terms_eligible(_session(None), _source("world_bank"))
    assert (caught.value.status_code, caught.value.detail) == (409, "provider_terms_ineligible")


async def test_open_provider_with_acknowledgement_returns_revision_to_capture() -> None:
    assert await provider_terms.require_terms_eligible(_session(_row()), _source("world_bank")) == 3


async def test_incompatible_use_is_denied() -> None:
    row = _row(provider_id="alpha_vantage", declared_use="commercial")
    with pytest.raises(HTTPException) as caught:
        await provider_terms.require_terms_eligible(_session(row), _source("alpha_vantage"))
    assert caught.value.detail == "provider_terms_ineligible"


async def test_review_provider_needs_operator_approval_for_acknowledged_version() -> None:
    pending = _row(provider_id="binance", declared_use="noncommercial")
    with pytest.raises(HTTPException):
        await provider_terms.require_terms_eligible(_session(pending), _source("binance"))
    approved = _row(
        provider_id="binance", declared_use="noncommercial", operator_review_state="approved",
        reviewer_user_id=9, reviewed_at=datetime.now(UTC), reviewed_allowed_use="noncommercial",
        review_evidence_ref="ticket-1", reviewed_terms_version="v1")
    assert await provider_terms.require_terms_eligible(_session(approved), _source("binance")) == 3
    stale = _row(
        provider_id="binance", declared_use="noncommercial", terms_version="v2", operator_review_state="approved",
        reviewer_user_id=9, reviewed_at=datetime.now(UTC), reviewed_allowed_use="noncommercial",
        review_evidence_ref="ticket-1", reviewed_terms_version="v1")
    with pytest.raises(HTTPException):
        await provider_terms.require_terms_eligible(_session(stale), _source("binance"))


async def test_terms_row_query_is_workspace_predicated() -> None:
    session = _session(None)
    await provider_terms._terms_row(session, _source("world_bank"))
    sql = str(session.scalar.await_args.args[0].compile(dialect=postgresql.dialect()))
    assert "connector_provider_terms.workspace_id =" in sql and "connector_provider_terms.source_id =" in sql


def test_owner_dto_cannot_carry_operator_review_fields() -> None:
    ProviderTermsAck = provider_terms.ProviderTermsAcknowledge
    ProviderTermsAck(declared_use="personal", terms_version="2026-10")
    for forbidden in ("operator_review_state", "reviewed_allowed_use", "review_evidence_ref", "terms_revision"):
        with pytest.raises(ValidationError):
            ProviderTermsAck(declared_use="personal", terms_version="v1", **{forbidden: "approved"})


async def test_operator_review_requires_operator_admission() -> None:
    body = provider_terms.OperatorTermsReview(
        state="approved", allowed_use="personal", evidence_ref="t-1", reviewed_terms_version="v1")
    for flag in (False, None, 1):
        with pytest.raises(PermissionError):
            await provider_terms.record_operator_review(
                _session(_row()), workspace_id=WORKSPACE_ID, source_id=SOURCE_ID, reviewer_user_id=1,
                body=body, instance_operator=flag)  # type: ignore[arg-type]


async def test_operator_review_binds_to_acknowledged_version_and_bumps_revision() -> None:
    row = _row(provider_id="binance", declared_use="noncommercial")
    session = _session(row)
    wrong = provider_terms.OperatorTermsReview(
        state="approved", allowed_use="noncommercial", evidence_ref="t-1", reviewed_terms_version="v0")
    with pytest.raises(HTTPException) as caught:
        await provider_terms.record_operator_review(
            session, workspace_id=WORKSPACE_ID, source_id=SOURCE_ID, reviewer_user_id=9, body=wrong,
            instance_operator=True)
    assert caught.value.status_code == 409
    no_use = provider_terms.OperatorTermsReview(state="approved", evidence_ref="t-1", reviewed_terms_version="v1")
    with pytest.raises(HTTPException):
        await provider_terms.record_operator_review(
            session, workspace_id=WORKSPACE_ID, source_id=SOURCE_ID, reviewer_user_id=9, body=no_use,
            instance_operator=True)
    good = provider_terms.OperatorTermsReview(
        state="approved", allowed_use="noncommercial", evidence_ref="t-1", reviewed_terms_version="v1")
    result = await provider_terms.record_operator_review(
        session, workspace_id=WORKSPACE_ID, source_id=SOURCE_ID, reviewer_user_id=9, body=good, instance_operator=True)
    assert result.eligible and row.terms_revision == 4 and row.reviewer_user_id == 9


async def test_owner_acknowledgement_change_bumps_revision_and_invalidates_review() -> None:
    row = _row(
        provider_id="binance", declared_use="noncommercial", operator_review_state="approved", reviewer_user_id=9,
        reviewed_at=datetime.now(UTC), reviewed_allowed_use="noncommercial", review_evidence_ref="t", reviewed_terms_version="v1")
    session = _session(row)
    scope = MagicMock()
    with (
        patch.object(provider_terms.connectors, "_connector_actor", return_value=1),
        patch.object(provider_terms.provisioning, "lock_connector", AsyncMock(return_value=(object(), None, {}))),
        patch.object(provider_terms.connectors, "_read_scoped_source", AsyncMock(return_value=_source("binance"))),
    ):
        result = await provider_terms.acknowledge_terms(
            session, SOURCE_ID, provider_terms.ProviderTermsAcknowledge(declared_use="noncommercial", terms_version="v2"),
            scope=scope, multi_workspace_enabled=True)
    assert row.terms_revision == 4 and row.terms_version == "v2"
    assert not result.eligible and result.decision == "operator_review_required"


async def test_scheduler_denies_before_inserting_a_request_and_captures_revision() -> None:
    session = MagicMock(scalar=AsyncMock(return_value=None), add=MagicMock(), flush=AsyncMock())
    scope = SimpleNamespace(workspace_id=WORKSPACE_ID, membership_revision=1)
    provisioning_row = SimpleNamespace(
        state="active", source_generation=1, applied_revision=2, desired_revision=2, backend_revision=1,
        execution_backend="native")

    async def run(provider: str, terms: Any) -> Any:
        with (
            patch.object(scheduler.connectors, "_connector_access", AsyncMock()),
            patch.object(scheduler.connectors, "_connector_actor", return_value=1),
            patch.object(scheduler.connectors, "require_collection_fence", AsyncMock(return_value=True)),
            patch.object(scheduler.provisioning, "lock_connector", AsyncMock(return_value=(object(), provisioning_row, {}))),
            patch("modules.sources.public.get_connector_source", AsyncMock(return_value=_source(provider))),
            patch.object(scheduler.provider_terms, "_terms_row", AsyncMock(return_value=terms)),
        ):
            return await scheduler._open_request(
                session, scope, SOURCE_ID, "manual", 2, multi_workspace_enabled=True)  # type: ignore[arg-type]

    with pytest.raises(HTTPException) as caught:
        await run("world_bank", None)
    assert caught.value.detail == "provider_terms_ineligible"
    session.add.assert_not_called()
    request = await run("world_bank", _row())
    added = session.add.call_args.args[0]
    assert isinstance(added, ConnectorCollectionRequest) and added.terms_revision == 3 and request is added


async def test_operator_review_route_uses_bootstrap_operator_admission_and_clears_terms_gate(monkeypatch):
    from types import SimpleNamespace
    from uuid import uuid4

    from core.auth.dependencies import require_owner_write
    from modules.connectors import routes

    route = next(r for r in routes.operator_router.routes if r.path.endswith("/terms-review"))
    assert require_owner_write in {d.call for d in route.dependant.dependencies}
    calls = []

    async def review(session, **kwargs):
        calls.append(("review", kwargs))
        return SimpleNamespace(terms_revision=7)

    async def clear(session, source_id, **kwargs):
        calls.append(("clear", kwargs))

    class Session:
        async def rollback(self): calls.append(("rollback", {}))
        async def commit(self): calls.append(("commit", {}))

    monkeypatch.setattr(routes.provider_terms, "record_operator_review", review)
    monkeypatch.setattr(routes.scheduler, "clear_collection_block", clear)
    ws, src = uuid4(), uuid4()
    result = await route.endpoint(ws, src, "body", Session(), SimpleNamespace(owner_id=1))
    assert result.terms_revision == 7
    assert calls[1][1] == {"workspace_id": ws, "source_id": src, "reviewer_user_id": 1, "body": "body", "instance_operator": True}
    assert calls[2] == ("clear", {"terms_revision": 7}) and calls[-1][0] == "commit"
