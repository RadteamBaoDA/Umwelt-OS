from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient

from core.api_headers import API_DEFAULT_CSP, ApiDefaultHeadersMiddleware


def _client() -> TestClient:
    app = FastAPI()
    app.add_middleware(ApiDefaultHeadersMiddleware)

    @app.get("/plain")
    def plain() -> PlainTextResponse:
        return PlainTextResponse("ok")

    @app.get("/own")
    def own() -> PlainTextResponse:
        return PlainTextResponse(
            "ok", headers={"Content-Security-Policy": "default-src 'none'; sandbox"}
        )

    return TestClient(app)


def test_default_csp_and_nosniff_added() -> None:
    r = _client().get("/plain")
    assert r.headers["content-security-policy"] == API_DEFAULT_CSP
    assert r.headers["x-content-type-options"] == "nosniff"


def test_existing_csp_not_overridden() -> None:
    assert _client().get("/own").headers["content-security-policy"] == "default-src 'none'; sandbox"


def test_unknown_route_404_also_covered() -> None:
    assert _client().get("/missing").headers["content-security-policy"] == API_DEFAULT_CSP
