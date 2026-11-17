"""DomainError must render byte-identically to HTTPException through the installed handlers."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from core import errors
from core.errors import (
    ConflictError,
    DomainError,
    ForbiddenError,
    NotFoundError,
    PayloadTooLargeError,
    RateLimitedError,
    UnavailableError,
    ValidationFailedError,
    error_detail,
    error_status,
)

CASES: list[tuple[type[DomainError], int, Any, dict[str, str] | None]] = [
    (NotFoundError, 404, "missing", None),
    (ConflictError, 409, "stale", None),
    (ForbiddenError, 403, "no", None),
    (ValidationFailedError, 422, "bad", None),
    (PayloadTooLargeError, 413, "big", None),
    (RateLimitedError, 429, "slow down", {"Retry-After": "7"}),
    (
        UnavailableError,
        503,
        {"code": "BUSY", "message": "try later", "details": {"a": 1}},
        {"Retry-After": "5"},
    ),
    (NotFoundError, 404, None, None),
]


def _http_raiser(status: int, detail: Any, headers: dict[str, str] | None) -> Callable[[], None]:
    """Return a route body raising the HTTPException flavour."""

    def raiser() -> None:
        raise HTTPException(status_code=status, detail=detail, headers=headers)

    return raiser


def _domain_raiser(
    cls: type[DomainError], detail: Any, headers: dict[str, str] | None
) -> Callable[[], None]:
    """Return a route body raising the DomainError flavour."""

    def raiser() -> None:
        raise cls(detail, headers=headers)

    return raiser


def _app() -> FastAPI:
    """Build an app with one HTTP and one domain route per case."""
    app = FastAPI()
    errors.install_error_handling(app)
    for index, (cls, status, detail, headers) in enumerate(CASES):
        app.add_api_route(f"/http/{index}", _http_raiser(status, detail, headers))
        app.add_api_route(f"/domain/{index}", _domain_raiser(cls, detail, headers))
    return app


@pytest.mark.parametrize("index", range(len(CASES)))
def test_domain_error_response_is_byte_identical(index: int) -> None:
    """Status, headers (minus per-request ID) and body bytes match for both exception families."""
    client = TestClient(_app(), raise_server_exceptions=False)
    left = client.get(f"/http/{index}")
    right = client.get(f"/domain/{index}")
    assert left.status_code == right.status_code == CASES[index][1]
    strip = {"x-request-id", "content-length"}
    assert {k: v for k, v in left.headers.items() if k not in strip} == {
        k: v for k, v in right.headers.items() if k not in strip
    }
    # Request IDs are generated per request; normalise before comparing bytes.
    assert left.content.replace(
        left.headers["x-request-id"].encode(), b"RID"
    ) == right.content.replace(right.headers["x-request-id"].encode(), b"RID")


def test_error_status_and_detail_helpers() -> None:
    """The helper treats both families uniformly and ignores unrelated exceptions."""
    assert error_status(HTTPException(status_code=404)) == 404
    assert error_status(NotFoundError("x")) == 404
    assert error_status(RuntimeError("x")) is None
    assert error_detail(ConflictError("c")) == "c"
    assert error_detail(ValueError()) is None


def test_default_detail_matches_http_phrase() -> None:
    """An omitted detail uses the standard reason phrase exactly like Starlette."""
    assert NotFoundError().detail == HTTPException(status_code=404).detail == "Not Found"
    assert DomainError("x", status_code=418).status_code == 418
