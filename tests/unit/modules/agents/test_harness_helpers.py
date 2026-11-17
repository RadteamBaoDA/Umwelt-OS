"""Pure-helper and factory tests for the decomposed agent harness."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from modules.agents import gateway_factory, harness
from modules.agents.gateway_factory import ModelGatewayFactory


def _config(**over: Any) -> SimpleNamespace:
    """Build a minimal resolved-config double."""
    base = {
        "gateway_base_url": "https://x/v1",
        "gateway_api_key": "k",
        "endpoint_destination_id": "dest",
        "gateway_identity": "g",
        "model_identity": "m",
        "configuration_revision": 3,
        "allow_public_providers": True,
        "gateway_kind": "openai",
    }
    return SimpleNamespace(**{**base, **over})


async def _noop() -> None:
    """Satisfy the required egress hook."""


def test_factory_passes_every_gateway_argument(monkeypatch: pytest.MonkeyPatch) -> None:
    """The factory forwards all config-derived arguments plus the bound run handles."""
    seen: dict[str, Any] = {}
    monkeypatch.setattr(gateway_factory, "ModelGateway", lambda **kw: seen.update(kw) or "gw")
    monkeypatch.setattr(gateway_factory.settings_public, "capability_evidence", lambda *_a: "ev")
    factory = ModelGatewayFactory.for_settings("sf", "redis", "scope", ["10.0.0.0/8"])  # type: ignore[arg-type]
    assert factory.create(_config(), timeout_seconds=7.0, before_send=_noop) == "gw"  # type: ignore[comparison-overlap]
    assert seen == {
        "evidence": "ev",
        "redis": "redis",
        "base_url": "https://x/v1",
        "api_key": "k",
        "destination_id": "dest",
        "timeout_seconds": 7.0,
        "scope": "scope",
        "gateway_identity": "g",
        "model_identity": "m",
        "configuration_revision": 3,
        "before_send": _noop,
        "approved_endpoint_cidrs": ("10.0.0.0/8",),
        "allow_public_endpoints": True,
        "kind": "openai",
    }


def test_factory_overrides_and_skips_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    """Destination/base URL overrides apply and ``with_evidence=False`` omits the evidence kwarg."""
    seen: dict[str, Any] = {}
    monkeypatch.setattr(gateway_factory, "ModelGateway", lambda **kw: seen.update(kw))
    factory = ModelGatewayFactory.for_settings("sf", "redis", "scope", [])  # type: ignore[arg-type]
    factory.create(
        _config(endpoint_destination_id=None),
        timeout_seconds=1.0,
        before_send=_noop,
        destination_id="remote",
        base_url="http://localhost:8000",
        with_evidence=False,
    )
    assert (
        "evidence" not in seen
        and seen["destination_id"] == "remote"
        and seen["base_url"] == "http://localhost:8000"
    )
    seen.clear()
    factory.create(
        _config(endpoint_destination_id=None),
        timeout_seconds=1.0,
        before_send=_noop,
        with_evidence=False,
    )
    assert seen["destination_id"] == ""


@pytest.mark.parametrize(
    ("state_value", "profile_value", "expected"),
    [
        (None, None, harness.DEFAULT_SPECIALIST_MAX_TOKENS),
        (500, None, 500),
        (None, 700, 700),
        (500, 700, 500),
        (0, None, harness.DEFAULT_SPECIALIST_MAX_TOKENS),
        (5000, None, harness.DEFAULT_SPECIALIST_MAX_TOKENS),
        (True, None, harness.DEFAULT_SPECIALIST_MAX_TOKENS),
        ("9", None, harness.DEFAULT_SPECIALIST_MAX_TOKENS),
    ],
)
def test_resolve_max_tokens(state_value: Any, profile_value: Any, expected: int) -> None:
    """Per-run beats profile beats default, and only ints in 1..4096 are honoured."""
    context = MagicMock()
    context.profile_snapshot = None if profile_value is None else {"max_tokens": profile_value}
    state: Any = {} if state_value is None else {"max_tokens": state_value}
    assert harness._resolve_max_tokens(context, state) == expected


def _call(call_id: Any, name: str = "t", arguments: Any = "{}") -> dict[str, Any]:
    """Build a gateway tool-call item."""
    item: dict[str, Any] = {"type": "function", "function": {"name": name, "arguments": arguments}}
    if call_id is not None:
        item["id"] = call_id
    return item


def test_parse_tool_calls_assigns_non_colliding_ids() -> None:
    """Missing IDs become ``call-N`` and skip IDs the model already used."""
    parsed = harness._parse_tool_calls([_call(None), _call("call-2"), _call(None)], {"t"})
    assert [item["call_id"] for item in parsed] == ["call-1", "call-2", "call-3"]


@pytest.mark.parametrize(
    "requested",
    [
        "nope",
        [_call("a"), _call("a")],
        [_call("a", name="other")],
        [_call("a", arguments=5)],
        ["x"],
        [{"id": "a", "function": "bad"}],
        [_call("a")] * (harness.MAX_TOOL_CALLS + 1),
        [_call("a", arguments="x" * (harness.MAX_TOOL_ARGUMENT_BYTES + 1))],
    ],
)
def test_parse_tool_calls_rejects_invalid(requested: Any) -> None:
    """Malformed lists, duplicate IDs, unknown names and oversize arguments raise ValueError."""
    with pytest.raises(ValueError):
        harness._parse_tool_calls(requested, {"t"})


def test_accumulate_token_usage_overflow_is_unknown() -> None:
    """Reaching 2**31 total tokens clears the total and marks usage unknown."""
    state: Any = {"token_usage": 2**31 - 2}
    assert harness._accumulate_token_usage(state, {"usage": {"total_tokens": 5}}) == (None, False)
    assert harness._accumulate_token_usage({"token_usage": 1}, {"usage": {"total_tokens": 2}}) == (
        3,
        True,
    )
    assert harness._accumulate_token_usage({"token_usage": 1}, {"usage": "bad"}) == (1, False)


def test_merge_handoff_usage() -> None:
    """Handoff usage adds to the parent and the unknown flag is sticky."""
    assert harness._merge_handoff_usage({}, {}) == {}  # type: ignore[typeddict-item]
    state: Any = {"token_usage": None, "token_usage_unknown": False}
    assert harness._merge_handoff_usage(state, {"tokens": 4, "unknown": False}) == {
        "token_usage": 4,
        "token_usage_unknown": False,
    }
    assert (
        harness._merge_handoff_usage(state, {"tokens": None, "unknown": True})[
            "token_usage_unknown"
        ]
        is True
    )


def test_decode_tool_arguments() -> None:
    """Valid objects pass through; everything else is invalid_arguments with empty/kept args."""
    assert harness._decode_tool_arguments('{"a": 1}') == ({"a": 1}, None)
    assert harness._decode_tool_arguments("[1]") == ({}, "invalid_arguments")
    assert harness._decode_tool_arguments("{") == ({}, "invalid_arguments")
    assert harness._decode_tool_arguments(None) == ({}, "invalid_arguments")
    assert harness._decode_tool_arguments('{"a": NaN}')[1] == "invalid_arguments"
