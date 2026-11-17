"""POST /{source_id}/rest-sample: prechecks, SSRF refusal, auth refusal and response pruning (no network/DB)."""

import json
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException, Request

from modules.connectors import provisioning
from modules.connectors.providers import rest
from modules.connectors.provisioning._routes import config as routes
from modules.connectors.provisioning._routes.common import DraftValidationRequest
from modules.sources.schemas import ConnectorSource

URL = "https://api.example.com/items?token=secret-in-url"
CONFIG = {"url": URL, "items_path": "$.items"}


def _source(**overrides: Any) -> ConnectorSource:
    """Build an active custom REST source."""
    values: dict[str, Any] = {
        "id": uuid4(),
        "workspace_id": uuid4(),
        "type": "api",
        "status": "active",
        "generation": 3,
        "configuration": {},
        "local_only": False,
        "provider": None,
        "graph_enabled": False,
        "rag_index_enabled": False,
    }
    return ConnectorSource(**{**values, **overrides})


def _payload(**overrides: Any) -> DraftValidationRequest:
    """Build a revision-0 draft body for generation 3."""
    body: dict[str, Any] = {
        "expected_revision": 0,
        "expected_source_generation": 3,
        "configuration": CONFIG,
        **overrides,
    }
    return DraftValidationRequest.model_validate(body)


@pytest.fixture
def patched(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Stub owner access, source lookup, activation row, URL policy and the bounded fetch."""
    state = SimpleNamespace(source=_source(), fetch=AsyncMock(), url_check=AsyncMock())
    state.fetch.return_value = rest.Fetched(b'{"items": []}')
    monkeypatch.setattr(routes, "_owner_access", AsyncMock(return_value=object()))
    monkeypatch.setattr(routes, "_source", AsyncMock(side_effect=lambda *a, **k: state.source))
    monkeypatch.setattr(provisioning, "activation_status", AsyncMock(return_value=None))
    monkeypatch.setattr(routes, "validate_public_url", state.url_check)
    monkeypatch.setattr(rest, "fetch_bounded", state.fetch)
    return state


async def _call(payload: DraftValidationRequest) -> Any:
    """Invoke the route function directly with a stub request/session."""
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(settings=SimpleNamespace(multi_workspace_enabled=False))
        )
    )
    return await routes.fetch_rest_sample(
        uuid4(),
        payload,
        AsyncMock(),
        cast(Request, request),
        cast(Any, SimpleNamespace(role="owner")),
    )


async def test_success_returns_only_status_truncated_sample(patched: SimpleNamespace) -> None:
    """The response never echoes headers, URL or secrets."""
    result = await _call(_payload())
    assert result.model_dump() == {"status_code": 200, "truncated": False, "sample": {"items": []}}
    kwargs = patched.fetch.await_args.kwargs
    assert kwargs["max_bytes"] == 256 * 1024 and kwargs["timeout"] == 10.0
    assert "secret-in-url" not in result.model_dump_json()


async def test_private_url_refused(patched: SimpleNamespace) -> None:
    """validate_public_url rejection stops before any fetch."""
    patched.url_check.side_effect = ValueError("private")
    with pytest.raises(HTTPException) as err:
        await _call(_payload())
    assert err.value.status_code == 422 and err.value.detail == "rest_sample_url_refused"
    patched.fetch.assert_not_awaited()


async def test_unsafe_destination_at_fetch_refused(patched: SimpleNamespace) -> None:
    """DNS-time loopback/private resolution inside fetch_bounded maps to the same 422."""
    patched.fetch.side_effect = rest.UnsafeDestination("non_public_destination")
    with pytest.raises(HTTPException) as err:
        await _call(_payload())
    assert err.value.status_code == 422 and err.value.detail == "rest_sample_url_refused"


async def test_header_auth_refused(patched: SimpleNamespace) -> None:
    """Header-authenticated drafts are refused with a stable code and never fetched."""
    with pytest.raises(HTTPException) as err:
        await _call(_payload(auth_method="http_header", auth_header_name="X-Key"))
    assert err.value.status_code == 422 and err.value.detail == "rest_sample_auth_unsupported"
    patched.fetch.assert_not_awaited()


@pytest.mark.parametrize("overrides", [{"type": "rss"}, {"provider": "github"}])
async def test_non_custom_rest_refused(patched: SimpleNamespace, overrides: dict[str, Any]) -> None:
    """Only provider-less api sources may be sampled."""
    patched.source = _source(**overrides)
    with pytest.raises(HTTPException) as err:
        await _call(_payload())
    assert err.value.status_code == 409
    patched.fetch.assert_not_awaited()


async def test_oversize_and_non_json_errors(patched: SimpleNamespace) -> None:
    """Oversize and non-JSON bodies yield stable 422 codes."""
    patched.fetch.side_effect = rest.CollectionIncomplete()
    with pytest.raises(HTTPException) as err:
        await _call(_payload())
    assert err.value.detail == "rest_sample_too_large"
    patched.fetch.side_effect = None
    patched.fetch.return_value = rest.Fetched(b"<html>")
    with pytest.raises(HTTPException) as err:
        await _call(_payload())
    assert err.value.status_code == 422 and err.value.detail == "rest_sample_not_json"


def test_prune_limits() -> None:
    """Depth, array length and string length are bounded and flagged as truncated."""
    assert routes._prune_sample({"a": [1, 2]}) == ({"a": [1, 2]}, False)
    arr, cut = routes._prune_sample(list(range(50)))
    assert len(arr) == 20 and cut
    text, cut = routes._prune_sample("x" * 400)
    assert len(text) == 300 and cut
    deep: Any = "leaf"
    for _ in range(10):
        deep = {"n": deep}
    pruned, cut = routes._prune_sample(json.loads(json.dumps(deep)))
    depth = 0
    while isinstance(pruned, dict):
        pruned, depth = pruned["n"], depth + 1
    assert depth == 6 and pruned is None and cut
