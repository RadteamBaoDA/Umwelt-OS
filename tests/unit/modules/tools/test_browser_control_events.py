"""Lifecycle callbacks of the isolated browser service: guard identity, permits and cleanup."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from modules.tools import browser_control
from tests.unit.modules.tools.test_browser_scope import (
    FENCE,
    SimpleNamespace_scope,
    _control_job,
    _control_request,
)


def _event(job: SimpleNamespace, event: str, **over: Any) -> Any:
    """Build a control event bound to ``job``'s operation and claim."""
    values: dict[str, Any] = {
        "event": event,
        "operation_id": job.operation_id,
        "claim_generation": 1,
        "service_instance_id": "svc-1",
        "request_ordinal": 0,
        "target_url": None,
        "actual_pages": 0,
        "actual_bytes": 0,
        "result_hash": None,
    }
    values.update(over)
    return browser_control.BrowserControlEvent(**values)


def _guard(job: SimpleNamespace, **over: Any) -> SimpleNamespace:
    """Build a remote guard matching the job's instance and nonce."""
    values: dict[str, Any] = {
        "state": "active",
        "service_instance_id": "svc-1",
        "remote_job_id": job.id,
        "nonce_hash": hashlib.sha256(("ab" * 32).encode("ascii")).hexdigest(),
        "expires_at": datetime.now(UTC) + timedelta(seconds=30),
    }
    values.update(over)
    return SimpleNamespace(**values)


@pytest.fixture
def seams(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Patch authority, the guard store and the commit to recording spies."""
    state: dict[str, Any] = {"events": [], "guard": None, "valid": True}
    grant = SimpleNamespace(origin="https://example.com", path_prefix="/")
    monkeypatch.setattr(browser_control, "_browser_network_verified", lambda: True)
    monkeypatch.setattr(
        browser_control,
        "_current_authority",
        AsyncMock(
            side_effect=lambda *a, **k: (
                state["valid"],
                grant if state["valid"] else None,
                SimpleNamespace_scope(),
                FENCE,
            )
        ),
    )
    monkeypatch.setattr(
        browser_control,
        "get_remote_heavy_guard",
        AsyncMock(side_effect=lambda *a, **k: state["guard"]),
    )
    monkeypatch.setattr(browser_control, "cleanup_proof_after_ack", lambda *a: "proof")
    monkeypatch.setattr(
        browser_control,
        "clear_remote_heavy_in_uow",
        AsyncMock(side_effect=lambda *a: state["events"].append("clear")),
    )
    monkeypatch.setattr(
        browser_control,
        "commit_with_replay",
        AsyncMock(side_effect=lambda *a, **k: state["events"].append("commit")),
    )
    return state


async def _post(job: SimpleNamespace, event: Any) -> Any:
    """Deliver ``event`` through the endpoint with a matching service credential."""
    request, _session, token = _control_request(job)
    return await browser_control.browser_control_event(
        job.id,
        event,
        request,
        authorization="Bearer svc",
        job_token=token,
    )


@pytest.mark.asyncio
async def test_authorize_grants_next_ordinal(seams: dict[str, Any]) -> None:
    """An in-scope request with the next ordinal is permitted and recorded."""
    job = _control_job()
    job.status, job.service_instance_id = "running", "svc-1"
    seams["guard"] = _guard(job)
    out = await _post(
        job, _event(job, "authorize", request_ordinal=1, target_url="https://example.com/a")
    )
    assert out == {"allowed": True, "request_ordinal": 1} and job.request_ordinal == 1
    assert seams["events"] == ["commit"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "override",
    [
        {"request_ordinal": 2},
        {"target_url": "https://evil.example/"},
        {"target_url": None},
    ],
)
async def test_authorize_denies_out_of_sequence_or_scope(
    seams: dict[str, Any], override: dict[str, Any]
) -> None:
    """A wrong ordinal or target is 403 and commits nothing."""
    job = _control_job()
    job.status, job.service_instance_id = "running", "svc-1"
    seams["guard"] = _guard(job)
    values = {"request_ordinal": 1, "target_url": "https://example.com/a", **override}
    with pytest.raises(HTTPException) as caught:
        await _post(job, _event(job, "authorize", **values))
    assert caught.value.status_code == 403 and seams["events"] == []


@pytest.mark.asyncio
async def test_authorize_with_revoked_authority_fails_job(seams: dict[str, Any]) -> None:
    """Lost authority fails the running job, commits, then answers 403."""
    job = _control_job()
    job.status, job.service_instance_id = "running", "svc-1"
    seams["guard"], seams["valid"] = _guard(job), False
    with pytest.raises(HTTPException) as caught:
        await _post(
            job, _event(job, "authorize", request_ordinal=1, target_url="https://example.com/a")
        )
    assert caught.value.status_code == 403 and (job.status, job.error_code) == (
        "failed",
        "authority_revoked",
    )
    assert seams["events"] == ["commit"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "guard_override", [{"service_instance_id": "other"}, {"nonce_hash": "x"}, {"state": "cleared"}]
)
async def test_guard_mismatch_is_conflict(
    seams: dict[str, Any], guard_override: dict[str, Any]
) -> None:
    """A guard for another instance/nonce (or a cleared one on authorize) is a 409."""
    job = _control_job()
    job.status, job.service_instance_id = "running", "svc-1"
    seams["guard"] = _guard(job, **guard_override)
    with pytest.raises(HTTPException) as caught:
        await _post(
            job, _event(job, "authorize", request_ordinal=1, target_url="https://example.com/a")
        )
    assert caught.value.status_code == 409
    assert caught.value.detail == "Browser remote guard is not active"


@pytest.mark.asyncio
async def test_missing_guard_is_conflict(seams: dict[str, Any]) -> None:
    """No registered guard means no permit or completion."""
    job = _control_job()
    job.status, job.service_instance_id = "running", "svc-1"
    with pytest.raises(HTTPException) as caught:
        await _post(job, _event(job, "complete", actual_pages=1, result_hash="a" * 64))
    assert caught.value.status_code == 409


@pytest.mark.asyncio
async def test_complete_records_totals_and_clears_guard(seams: dict[str, Any]) -> None:
    """A valid completion stores the totals, clears the guard after the ack and commits."""
    job = _control_job()
    job.status, job.service_instance_id = "running", "svc-1"
    job.max_pages, job.max_bytes = 3, 1000
    seams["guard"] = _guard(job)
    out = await _post(
        job, _event(job, "complete", actual_pages=2, actual_bytes=500, result_hash="b" * 64)
    )
    assert out == {"allowed": True} and (job.actual_pages, job.actual_bytes, job.result_hash) == (
        2,
        500,
        "b" * 64,
    )
    assert job.status == "running" and seams["events"] == ["clear", "commit"]


@pytest.mark.asyncio
async def test_complete_with_stale_totals_is_conflict(seams: dict[str, Any]) -> None:
    """Totals beyond the job's bounds are rejected without clearing the guard."""
    job = _control_job()
    job.status, job.service_instance_id = "running", "svc-1"
    job.max_pages, job.max_bytes = 3, 10
    seams["guard"] = _guard(job)
    with pytest.raises(HTTPException) as caught:
        await _post(
            job, _event(job, "complete", actual_pages=1, actual_bytes=500, result_hash="b" * 64)
        )
    assert caught.value.status_code == 409 and seams["events"] == []


@pytest.mark.asyncio
async def test_complete_for_cancelled_job_clears_without_resurrecting(
    seams: dict[str, Any],
) -> None:
    """A cancelled job answers cleaned and settles as cancelled instead of running."""
    job = _control_job()
    job.status, job.service_instance_id, job.cancel_requested = "running", "svc-1", True
    seams["guard"] = _guard(job)
    out = await _post(job, _event(job, "complete", actual_pages=1, result_hash="b" * 64))
    assert out == {"allowed": True, "cleaned": True} and job.status == "cancelled"
    assert seams["events"] == ["clear", "commit"]


@pytest.mark.asyncio
async def test_cancelled_event_and_idempotent_cleared_guard(seams: dict[str, Any]) -> None:
    """Cancelled marks the job cancelled and clears; a repeat on a cleared guard is a no-op."""
    job = _control_job()
    job.status, job.service_instance_id = "running", "svc-1"
    seams["guard"] = _guard(job, state="uncertain")
    assert await _post(job, _event(job, "cancelled")) == {"allowed": True}
    assert (job.status, job.cancel_requested) == ("cancelled", True) and seams["events"] == [
        "clear",
        "commit",
    ]
    seams["events"].clear()
    seams["guard"] = _guard(job, state="cleared")
    assert await _post(job, _event(job, "cancelled")) == {"allowed": True, "cleaned": True}
    assert seams["events"] == []


@pytest.mark.asyncio
async def test_stale_identity_and_expiry_are_conflicts(seams: dict[str, Any]) -> None:
    """A mismatched claim or an expired job on register/authorize is rejected before dispatch."""
    job = _control_job()
    job.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    with pytest.raises(HTTPException) as caught:
        await _post(job, _event(job, "register"))
    assert caught.value.status_code == 409
    job2 = _control_job()
    with pytest.raises(HTTPException) as caught2:
        await _post(job2, _event(job2, "register", operation_id=uuid4()))
    assert caught2.value.status_code == 409
