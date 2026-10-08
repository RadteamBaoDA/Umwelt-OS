"""The wrong-password 403 must carry the machine code `password_incorrect` in the error envelope."""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from core.auth.routes import _PASSWORD_INCORRECT
from core.errors import install_error_handling


def test_password_incorrect_envelope_has_machine_code() -> None:
    """Serializing the detail through the handler yields code=password_incorrect with status 403."""
    app = FastAPI()
    install_error_handling(app)

    @app.get("/x")
    def x() -> None:
        raise HTTPException(status_code=403, detail=_PASSWORD_INCORRECT)

    res = TestClient(app).get("/x")
    assert res.status_code == 403
    assert res.json()["error"]["code"] == "password_incorrect"
