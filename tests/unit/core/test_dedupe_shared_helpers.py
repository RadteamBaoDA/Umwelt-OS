"""Characterization tests for helpers consolidated from per-module copies (zero behaviour change)."""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import Response

from core.errors import ForbiddenError, ValidationFailedError
from core.http_cache import set_private_no_store
from core.model_gateway.factory import ModelGatewayFactory
from core.pagination import (
    b64url_decode,
    b64url_encode,
    decode_cursor,
    decode_fingerprinted_cursor,
    decode_scoped_offset,
)
from core.worker_context import worker_multi_workspace_flag, worker_session_factory
from core.workspaces.admission import actor_id, require_workspace_owner
from core.workspaces.schemas import InternalJobScope, WorkspaceContext

# Cursors recorded from the pre-consolidation implementations (hard-coded on purpose).
RECORDED_FINGERPRINTED = (
    "WyJhYmMxMjMiLCIyMDI2LTAxLTAyVDAzOjA0OjA1KzAwOjAwIiwiMDAwMDAwMDAtMDAwMC0wMDAwLTAwMDAtMDAwMDAwMDAwMDAxIl0"
)
RECORDED_PLAIN = "WyIyMDI2LTAxLTAyVDAzOjA0OjA1KzAwOjAwIiwiMDAwMDAwMDAtMDAwMC0wMDAwLTAwMDAtMDAwMDAwMDAwMDAxIl0"
RECORDED_OFFSET = "ZnA6NDA"
STAMP = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
ROW = UUID("00000000-0000-0000-0000-000000000001")
OWNER = WorkspaceContext(user_id=7, workspace_id=uuid4(), role="owner", membership_revision=1)
MEMBER = WorkspaceContext(user_id=8, workspace_id=OWNER.workspace_id, role="member", membership_revision=1)
JOB = InternalJobScope(workspace_id=OWNER.workspace_id, actor_user_id=7, membership_revision=1)


def test_recorded_cursors_decode_identically() -> None:
    """Cursors issued before the helper adoption still decode to the same values."""
    assert decode_fingerprinted_cursor(RECORDED_FINGERPRINTED, "abc123") == (STAMP, ROW)
    assert decode_cursor(RECORDED_PLAIN) == (STAMP, ROW)
    assert decode_scoped_offset(RECORDED_OFFSET, "fp", max_offset=100) == 40
    with pytest.raises(ValidationFailedError):
        decode_fingerprinted_cursor(RECORDED_FINGERPRINTED, "other")


def test_b64url_helpers_match_legacy_inline_expressions() -> None:
    """``b64url_encode``/``b64url_decode`` equal the removed inline base64 expressions."""
    for raw in (b"", b"a", b"ab", b"abc", b"\xfb\xff\xfe" * 7, RECORDED_PLAIN.encode()):
        legacy = base64.urlsafe_b64encode(raw).decode().rstrip("=")
        assert b64url_encode(raw) == legacy
        assert b64url_decode(legacy) == base64.b64decode(
            legacy + "=" * (-len(legacy) % 4), altchars=b"-_", validate=True)
    with pytest.raises(ValueError):
        b64url_decode("not base64!")


def test_actor_id_and_owner_requirement() -> None:
    """The canonical actor is the job actor or request user; members are denied with 403."""
    assert actor_id(OWNER) == 7 and actor_id(JOB) == 7
    require_workspace_owner(OWNER)
    require_workspace_owner(JOB)
    with pytest.raises(ForbiddenError) as denied:
        require_workspace_owner(MEMBER)
    assert denied.value.status_code == 403 and denied.value.detail == "Workspace owner required"
    with pytest.raises(TypeError, match="An explicit workspace scope is required"):
        require_workspace_owner(object())  # type: ignore[arg-type]


def test_worker_context_accessors() -> None:
    """Worker ctx helpers read the session factory and require a real boolean flag."""
    marker = object()
    assert worker_session_factory({"session_factory": marker}) is marker
    assert worker_multi_workspace_flag({"settings": SimpleNamespace(multi_workspace_enabled=True)}) is True
    with pytest.raises(TypeError, match="Actual workspace rollout flag required"):
        worker_multi_workspace_flag({"settings": SimpleNamespace(multi_workspace_enabled=1)})


def test_no_store_header() -> None:
    """Owner routes keep the exact private no-store cache directive."""
    response = Response()
    set_private_no_store(response)
    assert response.headers["Cache-Control"] == "private, no-store"


def test_gateway_factory_builds_expected_arguments() -> None:
    """The shared factory maps config fields exactly like the removed twelve-argument call sites."""
    seen: dict[str, Any] = {}
    config: Any = SimpleNamespace(
        gateway_base_url="http://gw", gateway_api_key="k", endpoint_destination_id=None,
        gateway_identity="gid", model_identity="mid", configuration_revision=3,
        allow_public_providers=False, gateway_kind="openai",
    )
    factory = ModelGatewayFactory.for_settings(
        "sf", "redis", JOB, ["10.0.0.0/8"],  # type: ignore[arg-type]
        evidence_provider=lambda sf, c: ("ev", sf),
        gateway_cls=lambda **kw: seen.update(kw) or "gw",
    )
    assert factory.create(config, timeout_seconds=5.0, before_send="hook", destination_id="remote") == "gw"  # type: ignore[arg-type]
    assert seen["evidence"] == ("ev", "sf") and seen["destination_id"] == "remote"
    assert seen["approved_endpoint_cidrs"] == ("10.0.0.0/8",) and seen["kind"] == "openai"
    seen.clear()
    factory.create(config, timeout_seconds=5.0, before_send="hook", with_evidence=False)  # type: ignore[arg-type]
    assert "evidence" not in seen and seen["destination_id"] == ""


# Cursors produced by the base-commit (a28b5da3) module encoders, hard-coded on purpose.
BASE_SOURCE_CURSOR = (
    "WzIsImZwMTIzIiwiV3lJeU1ESTJMVEF4TFRBeVZEQXpPakEwT2pBMUt6QXdPakF3SWl3aU1EQXdNREF3TURBdE1EQXdNQzB3TURBd0xUQXdNREF0TURBd01EQXdNREF3TURBeElsMCJd"
)
BASE_MEMORY_CURSOR = "eyJ2IjoyLCJiaW5kaW5nIjoiYmluZDEiLCJwb3NpdGlvbiI6IlBPUyJ9"
BASE_TOPIC_CURSOR = (
    "WzIsInRvcGljcyIsIjAwMDAwMDAwLTAwMDAtMDAwMC0wMDAwLTAwMDAwMDAwMDAwOSIsNywxLDIseyJpc19hY3RpdmUiOnRydWV9LCJjcmVhdGVkX2F0X2FzY19pZF9hc2MiLCIyMDI2LTAxLTAyVDAzOjA0OjA1KzAwOjAwIiwiMjAyNi0wMS0wMlQwMzowNDowNSswMDowMCIsIjAwMDAwMDAwLTAwMDAtMDAwMC0wMDAwLTAwMDAwMDAwMDAwMSJd"
)


def test_module_cursor_codecs_match_base_commit_recordings() -> None:
    """Rewritten module codecs decode base-commit cursors and re-encode them byte for byte."""
    from modules.memory.public import bind_page_cursor, unbind_page_cursor
    from modules.news.topics import _decode_topic_cursor, _encode_topic_cursor
    from modules.sources.public import _decode_source_page_cursor, _encode_source_page_cursor

    assert _decode_source_page_cursor(BASE_SOURCE_CURSOR, "fp123") == (STAMP, ROW)
    assert _encode_source_page_cursor(STAMP, ROW, "fp123") == BASE_SOURCE_CURSOR
    with pytest.raises(ValidationFailedError):
        _decode_source_page_cursor(BASE_SOURCE_CURSOR, "other")

    assert unbind_page_cursor(BASE_MEMORY_CURSOR, "bind1") == "POS"
    assert bind_page_cursor("POS", "bind1") == BASE_MEMORY_CURSOR
    with pytest.raises(ValidationFailedError):
        unbind_page_cursor(BASE_MEMORY_CURSOR, "other")

    fence: Any = SimpleNamespace(
        workspace_id=UUID(int=9), user_id=7, membership_revision=1, configuration_revision=2)
    assert _decode_topic_cursor(BASE_TOPIC_CURSOR, fence, True) == (STAMP, STAMP, ROW)
    assert _encode_topic_cursor(STAMP, STAMP, ROW, fence, True) == BASE_TOPIC_CURSOR
    with pytest.raises(ValueError):
        _decode_topic_cursor(BASE_TOPIC_CURSOR, fence, False)


def test_gateway_factory_source_forwards_kind_and_cidrs() -> None:
    """Guard moved with the construction site: ``create`` must forward ``kind`` and the CIDR allow-list."""
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(ModelGatewayFactory.create)))
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Attribute) and node.func.attr == "_new_gateway"]
    assert len(calls) == 1
    keywords = {kw.arg for kw in calls[0].keywords}
    assert {"kind", "approved_endpoint_cidrs", "allow_public_endpoints", "gateway_identity"} <= keywords
