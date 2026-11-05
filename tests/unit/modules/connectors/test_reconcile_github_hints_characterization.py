"""Characterization tests for ``reconcile_github_source_hints_lifecycle`` (rearm vs retire)."""

import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from core.workspaces.schemas import AccessFence, InternalJobScope
from modules.connectors import public as connectors

SOURCE_ID = uuid4()
WORKSPACE_ID = uuid4()
SCOPE = InternalJobScope(workspace_id=WORKSPACE_ID, actor_user_id=7, membership_revision=3,
                         source_id=SOURCE_ID, source_generation=2)
FENCE = AccessFence(WORKSPACE_ID, 7, 3, 5)
HINTS = "modules.connectors.service.github_hints"


class _Rows:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return list(self._rows)


class _Session:
    def __init__(self, scalar: list[Any], scalars: list[list[Any]]) -> None:
        self._scalar, self._scalars = list(scalar), list(scalars)
        self.log: list[str] = []

    async def scalar(self, stmt: object) -> Any:
        match = re.search(r"FROM (\w+)", str(stmt))
        self.log.append(f"scalar:{match.group(1) if match else '?'}")
        return self._scalar.pop(0)

    async def scalars(self, stmt: object) -> _Rows:
        match = re.search(r"FROM (\w+)", str(stmt))
        self.log.append(f"scalars:{match.group(1) if match else '?'}")
        return _Rows(self._scalars.pop(0))


def _hint(**over: Any) -> SimpleNamespace:
    base = {
        "repository_id": "11", "binding_revision": 6, "resource": "issue", "capacity_reserved": False,
        "state": "paused", "attempts": 3, "reconcile_page": 4, "claim_token": uuid4(), "claimed_revision": 1,
        "claim_expires_at": datetime.now(UTC), "source_generation": 1, "connector_revision": 1,
        "next_attempt_at": datetime.now(UTC) - timedelta(days=1),
    }
    return SimpleNamespace(**(base | over))


async def _run(session: _Session, *, active: bool, source_status: str = "active", generation: int = 2,
               binding_fence: Any = "default", provisioned: Any = "default") -> int:
    source = SimpleNamespace(generation=generation, status=source_status)
    fence = SimpleNamespace(repository_id="11", binding_revision=6, resource_scope=("issue",), connector_revision=4) \
        if binding_fence == "default" else binding_fence
    prov = SimpleNamespace(desired_revision=4) if provisioned == "default" else provisioned
    with patch(f"{HINTS}._read_scoped_source", AsyncMock(return_value=source)), \
            patch(f"{HINTS}._connector_access", AsyncMock(return_value=FENCE)), \
            patch("modules.sources.public.get_source_fence", AsyncMock(return_value=SimpleNamespace())), \
            patch("modules.connectors.provisioning._lock_connector_rows", AsyncMock(return_value=(None, prov, {}))), \
            patch(f"{HINTS}.lock_github_binding_fence_in_uow", AsyncMock(return_value=fence)):
        return await connectors.reconcile_github_source_hints_lifecycle(
            session, source_id=SOURCE_ID, source_generation=2, active=active,  # type: ignore[arg-type]
            scope=SCOPE, multi_workspace_enabled=False,
        )


@pytest.mark.asyncio
async def test_stale_generation_or_inactive_source_returns_zero_for_active_rearm() -> None:
    assert await _run(_Session([], []), active=True, generation=9) == 0
    assert await _run(_Session([], []), active=True, source_status="paused") == 0


@pytest.mark.asyncio
async def test_rearm_returns_zero_without_provisioning_binding_candidates_or_capacity() -> None:
    assert await _run(_Session([], []), active=True, provisioned=None) == 0
    assert await _run(_Session([], []), active=True, binding_fence=None) == 0
    s = _Session([], [[]])
    assert await _run(s, active=True) == 0 and s.log == ["scalars:github_source_hints"]
    s2 = _Session([None], [[_hint()]])
    assert await _run(s2, active=True) == 0 and s2.log == ["scalars:github_source_hints", "scalar:github_webhook_capacity"]


@pytest.mark.asyncio
async def test_rearm_reserves_capacity_defers_when_full_and_flags_unverified_hints() -> None:
    now = datetime.now(UTC)
    ok = _hint()
    reserved = _hint(capacity_reserved=True, state="capacity_deferred")
    foreign = _hint(repository_id="99", capacity_reserved=True)
    capacity = SimpleNamespace(pending_count=10)
    assert await _run(_Session([capacity], [[ok, reserved, foreign]]), active=True) == 2
    assert (ok.state, ok.capacity_reserved, ok.attempts, ok.reconcile_page) == ("pending", True, 0, 1)
    assert (reserved.state, reserved.capacity_reserved) == ("pending", True)
    assert (foreign.state, foreign.capacity_reserved) == ("visibility_unverified", False)
    assert capacity.pending_count == 10 + 1 - 1  # +1 for ``ok``, -1 released by ``foreign``
    assert ok.next_attempt_at >= now and ok.claim_token is None and ok.source_generation == 2 and ok.connector_revision == 4

    full = _hint()
    capacity_full = SimpleNamespace(pending_count=100_000)
    assert await _run(_Session([capacity_full], [[full]]), active=True) == 0
    assert full.state == "capacity_deferred" and full.capacity_reserved is False
    assert full.next_attempt_at > now + timedelta(seconds=30)
    wrong_resource = _hint(resource="release")
    assert await _run(_Session([SimpleNamespace(pending_count=1)], [[wrong_resource]]), active=True) == 0
    assert wrong_resource.state == "visibility_unverified"


@pytest.mark.asyncio
async def test_inactive_cleanup_locks_grant_hints_and_capacity_only_when_a_slot_is_owned() -> None:
    held = _hint(state="pending", capacity_reserved=True)
    capacity = SimpleNamespace(pending_count=5)
    s = _Session([SimpleNamespace(), None, capacity], [[held]])
    result = await _run(s, active=False, source_status="paused")
    assert s.log == ["scalar:connector_provisioning", "scalar:github_oauth_grants",
                     "scalars:github_source_hints", "scalar:github_webhook_capacity"]
    assert result == 1 and held.state == "paused" and held.capacity_reserved is False and capacity.pending_count == 4
    s2 = _Session([SimpleNamespace(), None], [[]])
    assert await _run(s2, active=False) == 0
    assert s2.log == ["scalar:connector_provisioning", "scalar:github_oauth_grants", "scalars:github_source_hints"]
    assert await _run(_Session([None], []), active=False) == 0
