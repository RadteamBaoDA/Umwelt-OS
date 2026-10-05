"""Unit tests for core error handling, middlewares, and exception handlers.

Tests request ID injection, cache headers, StarletteHTTPException mapping,
RequestValidationError redaction, and generic 500 error sanitization.
"""

from __future__ import annotations

from typing import Any
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field

from core.errors import install_error_handling


def create_test_app() -> FastAPI:
    """Build a test FastAPI instance with error handling installed and sample endpoints."""
    app = FastAPI()
    install_error_handling(app)

    class SampleBody(BaseModel):
        name: str = Field(min_length=3)
        secret_token: str

    @app.get("/api/v1/auth/session")
    def auth_session() -> dict[str, str]:
        return {"status": "authenticated"}

    @app.get("/api/v1/sources")
    def list_sources() -> list[str]:
        return ["source1", "source2"]

    @app.get("/public/ping")
    def public_ping() -> dict[str, str]:
        return {"ping": "pong"}

    @app.get("/api/v1/not-found")
    def not_found() -> None:
        raise HTTPException(status_code=404, detail="Resource not found")

    @app.get("/api/v1/custom-http-error")
    def custom_http_error() -> None:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "PERMISSION_DENIED",
                "message": "You lack access to this workspace",
                "details": {"required_role": "admin"},
            },
            headers={"X-Custom-Deny": "true"},
        )

    @app.post("/api/v1/items")
    def create_item(body: SampleBody) -> dict[str, Any]:
        return {"name": body.name}

    @app.get("/api/v1/crash")
    def crash_server() -> None:
        raise RuntimeError("Secret database connection string leaked internally!")

    return app


@pytest.fixture
def client() -> TestClient:
    """Provide a TestClient with error handling configured."""
    return TestClient(create_test_app(), raise_server_exceptions=False)


class TestRequestIdAndCacheControl:
    """Test suite for the request_id middleware and Cache-Control headers."""

    def test_request_id_header_injected_on_success(self, client: TestClient) -> None:
        """Every response contains an X-Request-ID header."""
        resp = client.get("/public/ping")
        assert resp.status_code == 200
        assert "X-Request-ID" in resp.headers
        assert len(resp.headers["X-Request-ID"]) > 10

    def test_auth_route_receives_no_store_cache_control(self, client: TestClient) -> None:
        """Endpoints under /api/v1/auth/ receive Cache-Control: no-store."""
        resp = client.get("/api/v1/auth/session")
        assert resp.status_code == 200
        assert resp.headers.get("Cache-Control") == "no-store"

    def test_api_route_receives_private_no_store_cache_control(self, client: TestClient) -> None:
        """Endpoints under /api/v1/ receive Cache-Control: private, no-store."""
        resp = client.get("/api/v1/sources")
        assert resp.status_code == 200
        assert resp.headers.get("Cache-Control") == "private, no-store"

    def test_non_api_route_does_not_force_api_cache_control(self, client: TestClient) -> None:
        """Routes outside /api/v1/ do not have private, no-store injected."""
        resp = client.get("/public/ping")
        assert resp.status_code == 200
        assert resp.headers.get("Cache-Control") is None


class TestHttpExceptionHandler:
    """Test suite for StarletteHTTPException serialization."""

    def test_standard_http_exception_envelope(self, client: TestClient) -> None:
        """Standard HTTPException with string detail maps to HTTP_{status} error envelope."""
        resp = client.get("/api/v1/not-found")
        assert resp.status_code == 404
        data = resp.json()

        assert "error" in data
        err = data["error"]
        assert err["code"] == "HTTP_404"
        assert err["message"] == "Resource not found"
        assert err["details"] == {}
        assert err["requestId"] == resp.headers.get("X-Request-ID")

    def test_structured_http_exception_envelope_and_headers(self, client: TestClient) -> None:
        """HTTPException with dict detail preserves custom error code, message, and details."""
        resp = client.get("/api/v1/custom-http-error")
        assert resp.status_code == 403
        assert resp.headers.get("X-Custom-Deny") == "true"
        data = resp.json()

        assert "error" in data
        err = data["error"]
        assert err["code"] == "PERMISSION_DENIED"
        assert err["message"] == "You lack access to this workspace"
        assert err["details"] == {"required_role": "admin"}
        assert err["requestId"] == resp.headers.get("X-Request-ID")


class TestValidationExceptionHandler:
    """Test suite for RequestValidationError sanitization."""

    def test_validation_error_strips_input_and_ctx(self, client: TestClient) -> None:
        """Request validation error sanitizes submitted sensitive input and context."""
        sensitive_payload = {
            "name": "a",  # min_length=3 failure
            "secret_token": "super_secret_credentials_12345",
        }
        resp = client.post("/api/v1/items", json=sensitive_payload)
        assert resp.status_code == 422
        data = resp.json()

        assert "error" in data
        err = data["error"]
        assert err["code"] == "VALIDATION_ERROR"
        assert err["message"] == "Request validation failed"
        assert err["requestId"] == resp.headers.get("X-Request-ID")

        details = err["details"]
        assert isinstance(details, list)
        assert len(details) > 0

        # Verify neither 'input' nor 'ctx' appear in any detail entry
        for detail_item in details:
            assert "input" not in detail_item
            assert "ctx" not in detail_item
            # Sensitive token value must never be echoed
            assert "super_secret_credentials_12345" not in str(detail_item)


class TestInternalExceptionHandler:
    """Test suite for unhandled exception sanitization."""

    def test_internal_error_does_not_leak_details(self, client: TestClient) -> None:
        """Unhandled exceptions return a safe generic 500 error envelope."""
        resp = client.get("/api/v1/crash")
        assert resp.status_code == 500
        data = resp.json()

        assert "error" in data
        err = data["error"]
        assert err["code"] == "INTERNAL_ERROR"
        assert err["message"] == "The request could not be completed"
        assert err["details"] == {}
        assert len(err["requestId"]) > 10
        # Verify requestId is a valid UUID
        from uuid import UUID
        UUID(err["requestId"])

        # Secret message must never leak to response body
        assert "Secret database connection string" not in resp.text
