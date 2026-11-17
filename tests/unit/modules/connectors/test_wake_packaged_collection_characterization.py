"""Characterization tests for ``wake_packaged_collection`` outcomes, fences and rollback discipline."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import httpx
import pytest

from core.workspaces.schemas import AccessFence, InternalJobScope
from modules.connectors import public as connectors

SOURCE_ID = uuid4()
WORKSPACE_ID = uuid4()
SCOPE = InternalJobScope(
    workspace_id=WORKSPACE_ID,
    actor_user_id=7,
    membership_revision=3,
    source_id=SOURCE_ID,
    source_generation=2,
)
FENCE = AccessFence(WORKSPACE_ID, 7, 3, 5)
PACKAGED = "modules.connectors.service.packaged"


def _response(
    status: int = 200, body: Any = None, headers: dict[str, str] | None = None
) -> MagicMock:
    response = MagicMock()
    response.status_code = status
    response.headers = headers or {}
    response.json = (
        MagicMock(return_value=body)
        if not isinstance(body, Exception)
        else MagicMock(side_effect=body)
    )
    if status >= 400 and status not in {409, 425, 429}:
        response.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError(
                "x", request=httpx.Request("POST", "http://n8n"), response=httpx.Response(status)
            )
        )
    return response


async def _wake(
    *,
    response: Any = None,
    post_error: Exception | None = None,
    source_ok: bool = True,
    provisioned: Any = "ok",
    enabled: Any = True,
    fence_ok: Any = True,
    token: str | None = "tok",
    n8n_enabled: bool = True,
    timeout: int = 15,  # noqa: ASYNC109  # test double mirrors the production signature
    current_same: bool = True,
    module_after: bool = True,
) -> tuple[Any, SimpleNamespace]:
    source = SimpleNamespace(status="active", generation=2, type="rss") if source_ok else None
    locked = SimpleNamespace(local_only=False)
    prov = (
        SimpleNamespace(state="active", applied_revision=4, desired_revision=4, source_generation=2)
        if provisioned == "ok"
        else provisioned
    )
    session = SimpleNamespace(rollback=AsyncMock())
    post = (
        AsyncMock(return_value=response)
        if post_error is None
        else AsyncMock(side_effect=post_error)
    )
    n8n = SimpleNamespace(enabled=n8n_enabled, webhook_token=token, service_url="http://n8n:5678")
    enabled_seq = [enabled] + ([module_after] if True else [])
    fence_calls = AsyncMock(side_effect=[fence_ok, fence_ok])
    with (
        patch(f"{PACKAGED}._connector_access", AsyncMock(return_value=FENCE)),
        patch(
            "modules.sources.public.lock_source",
            AsyncMock(
                side_effect=[locked, locked if current_same else SimpleNamespace(local_only=True)]
            ),
        ),
        patch("modules.sources.public.get_connector_source", AsyncMock(return_value=source)),
        patch("modules.connectors.provisioning.activation_status", AsyncMock(return_value=prov)),
        patch("modules.settings.public.module_is_enabled", AsyncMock(side_effect=enabled_seq)),
        patch(f"{PACKAGED}.require_collection_fence", fence_calls),
        patch(f"{PACKAGED}._read_scoped_source", AsyncMock(return_value=source)),
        patch("modules.settings.public.resolve_n8n", AsyncMock(return_value=n8n)),
        patch(f"{PACKAGED}._post_wake", post),
    ):
        result = await connectors.wake_packaged_collection(
            session,
            source_id=SOURCE_ID,
            source_generation=2,
            connector_revision=4,  # type: ignore[arg-type]
            settings=SimpleNamespace(),
            timeout_seconds=timeout,
            scope=SCOPE,
            multi_workspace_enabled=False,
        )
    return result, SimpleNamespace(session=session, post=post)


@pytest.mark.asyncio
async def test_timeout_range_is_validated_first() -> None:
    for bad in (0, 76):
        with pytest.raises(ValueError, match="timeout is out of range"):
            await _wake(timeout=bad)


@pytest.mark.asyncio
async def test_unavailable_when_source_provisioning_module_or_fence_do_not_match() -> None:
    for kwargs in (
        {"source_ok": False},
        {"provisioned": None},
        {"enabled": False},
        {"fence_ok": False},
        {
            "provisioned": SimpleNamespace(
                state="active", applied_revision=3, desired_revision=4, source_generation=2
            )
        },
    ):
        result, spies = await _wake(response=_response(), **kwargs)
        assert result.outcome == "unavailable" and spies.session.rollback.await_count >= 1
        spies.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_disabled_n8n_or_missing_token_is_unavailable_without_network() -> None:
    for kwargs in ({"token": None}, {"n8n_enabled": False}):
        result, spies = await _wake(response=_response(), **kwargs)
        assert result.outcome == "unavailable" and spies.post.await_count == 0
        assert (
            spies.session.rollback.await_count == 2
        )  # after the fence read and after n8n resolution


@pytest.mark.asyncio
async def test_deferral_statuses_carry_a_capped_retry_after() -> None:
    for status in (409, 425, 429):
        result, _ = await _wake(response=_response(status, headers={"retry-after": "30"}))
        assert result.outcome == "deferred" and result.next_eligible_at is not None
    capped, _ = await _wake(response=_response(429, headers={"retry-after": "999999"}))
    assert capped.next_eligible_at is not None
    none, _ = await _wake(response=_response(429, headers={"retry-after": "soon"}))
    assert none.outcome == "deferred" and none.next_eligible_at is None


@pytest.mark.asyncio
async def test_acknowledged_wake_parses_bounded_metadata() -> None:
    run_id, batch_id = uuid4(), uuid4()
    body = {
        "run_id": str(run_id),
        "batch_id": str(batch_id),
        "status": "s" * 50,
        "receipt_id": "r" * 200,
    }
    result, spies = await _wake(response=_response(200, body))
    assert (result.outcome, result.run_id, result.batch_id) == ("acknowledged", run_id, batch_id)
    assert result.status == "s" * 32 and result.receipt_id == "r" * 128
    assert (
        spies.session.rollback.await_count == 3
    )  # fence read, n8n resolve, post-send revalidation


@pytest.mark.asyncio
async def test_acknowledged_wake_tolerates_invalid_metadata() -> None:
    result, _ = await _wake(response=_response(200, {"run_id": "not-a-uuid", "status": 5}))
    assert (result.outcome, result.run_id, result.batch_id, result.status, result.receipt_id) == (
        "acknowledged",
        None,
        None,
        "queued",
        None,
    )
    result2, _ = await _wake(response=_response(200, ValueError("not json")))
    assert result2.outcome == "acknowledged" and result2.status == "queued"
    result3, _ = await _wake(response=_response(200, ["list"]))
    assert result3.outcome == "acknowledged"


@pytest.mark.asyncio
async def test_authority_change_during_the_send_makes_the_result_unavailable() -> None:
    result, _ = await _wake(response=_response(200, {}), module_after=False)
    assert result.outcome == "unavailable"
    result2, _ = await _wake(response=_response(200, {}), current_same=False)
    assert result2.outcome == "unavailable"


@pytest.mark.asyncio
async def test_transport_failures_map_to_ambiguous_or_unavailable() -> None:
    timeout, _ = await _wake(post_error=httpx.ReadTimeout("slow"))
    assert timeout.outcome == "ambiguous"
    refused, _ = await _wake(post_error=httpx.ConnectError("down"))
    assert refused.outcome == "unavailable"
    rejected, _ = await _wake(response=_response(500))
    assert rejected.outcome == "unavailable"
