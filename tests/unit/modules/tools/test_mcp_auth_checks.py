"""Pure request checks extracted from ``InboundMcpGuard.__call__``."""

from __future__ import annotations

import pytest

from modules.tools.mcp_auth import InboundMcpGuard, _Reject

JSON = (b"content-type", b"application/json")


@pytest.mark.parametrize(("method", "headers", "expected"), [
    ("POST", [JSON], None),
    ("POST", [(b"content-type", b"Application/JSON; charset=utf-8"), (b"content-length", b"10")], 10),
    ("GET", [JSON], _Reject(405, "method_not_allowed")),
    ("PUT", [JSON], _Reject(405, "method_not_allowed")),
    ("POST", [JSON, (b"content-encoding", b"gzip")], _Reject(415, "unsupported_encoding")),
    ("POST", [(b"content-type", b"text/plain")], _Reject(415, "unsupported_media_type")),
    ("POST", [], _Reject(415, "unsupported_media_type")),
    ("POST", [JSON, (b"content-length", b"1"), (b"content-length", b"1")], _Reject(400, "invalid_request")),
    ("POST", [JSON, (b"content-length", b"x")], _Reject(400, "invalid_request")),
    ("POST", [JSON, (b"content-length", b"-1")], _Reject(413, "request_too_large")),
    ("POST", [JSON, (b"content-length", b"128001")], _Reject(413, "request_too_large")),
])
def test_check_framing(method: str, headers: list[tuple[bytes, bytes]], expected: object) -> None:
    """Framing accepts only bounded identity-encoded JSON POSTs and names the exact rejection otherwise."""
    assert InboundMcpGuard._check_framing(headers, method) == expected


@pytest.mark.parametrize(("value", "expected"), [
    (b"Bearer abc", "abc"),
    (b"bearer abc", _Reject(401, "unauthorized", bearer_challenge=True)),
    (b"Bearer ", _Reject(401, "unauthorized", bearer_challenge=True)),
    (b"Bearer a b", _Reject(401, "unauthorized", bearer_challenge=True)),
    (b"Bearer \xff", _Reject(401, "unauthorized", bearer_challenge=True)),
    (b"Bearer " + b"a" * 513, _Reject(401, "unauthorized", bearer_challenge=True)),
])
def test_bearer_token(value: bytes, expected: object) -> None:
    """Only a short ASCII token without whitespace is extracted; everything else is a 401 challenge."""
    assert InboundMcpGuard._bearer_token([(b"authorization", value)]) == expected
