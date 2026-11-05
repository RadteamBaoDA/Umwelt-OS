"""Characterization tests for ``revoke_deleted_source_github_grants`` (three-phase grant revoke).

Pins phase ordering, lock order, coordinator claim/release and recorded outcomes through the public
entry point so the phase helpers extracted from it keep the same contract.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from core.errors import ForbiddenError
from core.workspaces.schemas import AccessFence, InternalJobScope
from modules.connectors import worker

SOURCE_ID = uuid4()
WORKSPACE_ID = uuid4()
SCOPE = InternalJobScope(workspace_id=WORKSPACE_ID, actor_user_id=7, membership_revision=3,
                         source_id=SOURCE_ID, source_generation=2)
FENCE = AccessFence(WORKSPACE_ID, 7, 3, 5)
DELETED = worker._DELETED_SOURCE_REVOKE


class _Result:
    def __init__(self, ids: list[Any]) -> None:
        self._ids = ids

    def scalars(self) -> "_Result":
        return self

    def all(self) -> list[Any]:
        return self._ids


class _Session:
    def __init__(self, log: list[str], index: int, scalar: list[Any] | None = None, ids: list[Any] | None = None) -> None:
        self.log, self.index, self._scalar, self._ids = log, index, list(scalar or []), ids or []
        self.added: list[Any] = []

    async def execute(self, stmt: object) -> _Result:
        self.log.append(f"{self.index}:execute")
        return _Result(self._ids)

    async def scalar(self, stmt: object) -> Any:
        self.log.append(f"{self.index}:scalar")
        return self._scalar.pop(0)

    def add(self, row: Any) -> None:
        self.log.append(f"{self.index}:add")
        self.added.append(row)

    async def flush(self) -> None:
        self.log.append(f"{self.index}:flush")

    async def commit(self) -> None:
        self.log.append(f"{self.index}:commit")

    async def rollback(self) -> None:
        self.log.append(f"{self.index}:rollback")


class _Factory:
    def __init__(self, scripts: list[dict[str, Any]]) -> None:
        self.log: list[str] = []
        self.sessions = [_Session(self.log, i, **s) for i, s in enumerate(scripts)]
        self.opened = 0

    def __call__(self) -> "_Factory":
        return self

    async def __aenter__(self) -> _Session:
        self.opened += 1
        return self.sessions[self.opened - 1]

    async def __aexit__(self, *exc: object) -> None:
        return None


def _grant() -> SimpleNamespace:
    return SimpleNamespace(
        encrypted_tokens=b"x", error_code=DELETED, source_id=SOURCE_ID, operation_id=uuid4(),
        source_generation=2, configuration_revision=4, token_revision=9,
    )


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        multi_workspace_enabled=False,
        connector_credential_encryption_key=SimpleNamespace(get_secret_value=lambda: "key"),
    )


async def _run(factory: _Factory, *, peer: bool = False, token: str | None = "tok", revoke: Any = None,
               finish: Any = None, admit: Any = None, lock: Any = None) -> tuple[int, dict[str, Any]]:
    spies: dict[str, Any] = {}
    spies["revoke"] = revoke or AsyncMock()
    spies["finish"] = finish or AsyncMock()
    spies["replay"] = AsyncMock()
    spies["lock"] = lock or AsyncMock(return_value=(SimpleNamespace(), None, {}))
    with patch.object(worker, "_read_cursor", AsyncMock(return_value=None)), \
            patch.object(worker, "_write_cursor", AsyncMock()), \
            patch.object(worker, "_admit_source_job", admit or AsyncMock(return_value=(SCOPE, FENCE))), \
            patch.object(worker.provisioning, "lock_connector", spies["lock"]), \
            patch.object(worker.provisioning, "github_grant_has_active_peer", AsyncMock(return_value=peer)), \
            patch.object(worker.provisioning, "finish_deleted_source_grant_revoke", spies["finish"]), \
            patch.object(worker, "commit_with_replay", spies["replay"]), \
            patch("modules.connectors.github.oauth._open_token_cipher", MagicMock(return_value={"access_token": token})), \
            patch("modules.connectors.github.oauth.revoke_github_grant", spies["revoke"]):
        handled = await worker.revoke_deleted_source_github_grants(factory, _settings())  # type: ignore[arg-type]
    return handled, spies


@pytest.mark.asyncio
async def test_revoke_happy_path_claims_coordinator_calls_provider_and_releases_claim() -> None:
    grant = _grant()
    claimed_ops: list[Any] = []
    coordinator = None  # created in phase 1
    factory = _Factory([{"ids": [SOURCE_ID]}, {"scalar": [grant, None]}, {"scalar": []}])
    phase3_coordinator = SimpleNamespace(operation_id=None, state=None, error_code=None)

    async def revoke(settings: object, token: str) -> None:
        claimed_ops.append((token, factory.sessions[1].added[0].operation_id))
        phase3_coordinator.operation_id = factory.sessions[1].added[0].operation_id

    factory.sessions[2]._scalar.append(phase3_coordinator)
    handled, spies = await _run(factory, revoke=AsyncMock(side_effect=revoke))
    assert handled == 1
    new_coordinator = factory.sessions[1].added[0]
    assert (new_coordinator.state, new_coordinator.error_code) == ("revoking", DELETED)
    assert claimed_ops[0][0] == "tok"
    assert factory.log == [
        "0:execute", "0:rollback", "1:scalar", "1:scalar", "1:add", "1:flush", "2:scalar",
    ]
    assert spies["finish"].await_args.args[2] == "provider_revoked_source_deleted"
    assert spies["finish"].await_args.kwargs["grant_operation_id"] == grant.operation_id
    assert spies["finish"].await_args.kwargs["token_revision"] == 9
    assert (phase3_coordinator.state, phase3_coordinator.operation_id, phase3_coordinator.error_code) == ("idle", None, None)
    assert spies["replay"].await_count == 2
    del coordinator


@pytest.mark.asyncio
async def test_revoke_failure_and_live_peer_record_distinct_outcomes() -> None:
    factory = _Factory([{"ids": [SOURCE_ID]}, {"scalar": [_grant(), SimpleNamespace(state="idle", operation_id=None, error_code=None)]},
                        {"scalar": [SimpleNamespace(operation_id=None)]}])
    handled, spies = await _run(factory, revoke=AsyncMock(side_effect=RuntimeError("provider down")))
    assert handled == 1 and spies["finish"].await_args.args[2] == "provider_revoke_failed_source_deleted"

    factory2 = _Factory([{"ids": [SOURCE_ID]}, {"scalar": [_grant(), SimpleNamespace(state="idle", operation_id=None, error_code=None)]}, {"scalar": []}])
    handled2, spies2 = await _run(factory2, peer=True)
    assert handled2 == 1 and spies2["finish"].await_args.args[2] == "provider_revoke_skipped_source_deleted"
    spies2["revoke"].assert_not_awaited()

    factory3 = _Factory([{"ids": [SOURCE_ID]}, {"scalar": [_grant(), SimpleNamespace(state="idle", operation_id=None, error_code=None)]}, {"scalar": []}])
    handled3, spies3 = await _run(factory3, token=None)
    assert handled3 == 1 and spies3["finish"].await_args.args[2] == "provider_revoke_failed_source_deleted"
    spies3["revoke"].assert_not_awaited()


@pytest.mark.asyncio
async def test_busy_coordinator_is_skipped_and_stale_own_claim_is_released() -> None:
    busy = SimpleNamespace(state="refreshing", operation_id=uuid4(), error_code=None, updated_at=datetime.now(UTC))
    factory = _Factory([{"ids": [SOURCE_ID]}, {"scalar": [_grant(), busy]}])
    handled, spies = await _run(factory)
    assert handled == 0 and factory.log[-1] == "1:rollback" and spies["revoke"].await_count == 0

    stale = SimpleNamespace(state="revoking", operation_id=uuid4(), error_code=DELETED,
                            updated_at=datetime.now(UTC) - timedelta(hours=1))
    factory2 = _Factory([{"ids": [SOURCE_ID]}, {"scalar": [_grant(), stale]}])
    handled2, spies2 = await _run(factory2)
    assert handled2 == 0 and spies2["replay"].await_count == 1
    assert (stale.state, stale.operation_id, stale.error_code) == ("idle", None, None)


@pytest.mark.asyncio
async def test_missing_anchor_or_unadmitted_source_is_skipped() -> None:
    factory = _Factory([{"ids": [SOURCE_ID]}, {"scalar": []}])
    handled, _ = await _run(factory, admit=AsyncMock(return_value=None))
    assert handled == 0 and factory.log == ["0:execute", "0:rollback"]

    factory2 = _Factory([{"ids": [SOURCE_ID]}, {"scalar": [_grant()]}])
    handled2, _ = await _run(factory2, lock=AsyncMock(return_value=(None, None, {})))
    assert handled2 == 0 and factory2.log[-1] == "1:rollback"


@pytest.mark.asyncio
async def test_denied_phase_one_is_skipped_and_other_errors_propagate() -> None:
    factory = _Factory([{"ids": [SOURCE_ID]}, {"scalar": []}])
    handled, _ = await _run(factory, lock=AsyncMock(side_effect=ForbiddenError("gone")))
    assert handled == 0 and factory.log[-1] == "1:rollback"

    from fastapi import HTTPException
    factory2 = _Factory([{"ids": [SOURCE_ID]}, {"scalar": []}])
    with pytest.raises(HTTPException):
        await _run(factory2, lock=AsyncMock(side_effect=HTTPException(status_code=500, detail="x")))


@pytest.mark.asyncio
async def test_denied_phase_three_undoes_own_claim_without_recording() -> None:
    phase3_coordinator = SimpleNamespace(operation_id=None, state="revoking", error_code=DELETED)
    phase1_coordinator = SimpleNamespace(state="idle", operation_id=None, error_code=None)
    factory = _Factory([{"ids": [SOURCE_ID]}, {"scalar": [_grant(), phase1_coordinator]}, {"scalar": [phase3_coordinator]}])

    async def revoke(settings: object, token: str) -> None:
        phase3_coordinator.operation_id = phase1_coordinator.operation_id

    handled, _ = await _run(factory, revoke=AsyncMock(side_effect=revoke), finish=AsyncMock(side_effect=ForbiddenError("lost")))
    assert handled == 0
    assert factory.log[-3:] == ["2:rollback", "2:scalar", "2:commit"]
    assert (phase3_coordinator.state, phase3_coordinator.operation_id) == ("idle", None)
