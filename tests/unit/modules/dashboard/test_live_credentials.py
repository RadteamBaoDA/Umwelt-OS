"""Live map credential envelopes are scoped, redacted and strictly bounded."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from pydantic import SecretStr, ValidationError

from core.workspaces.schemas import WorkspaceContext
from modules.dashboard.live_credentials import (
    LiveCredentials,
    LiveCredentialUpdate,
    _open_secrets,
    _seal_secrets,
    require_live_terms,
    save_live_credentials,
)


def test_encrypted_secret_is_bound_to_workspace_and_revision() -> None:
    key, workspace = Fernet.generate_key().decode(), uuid4()
    encrypted = _seal_secrets(key, workspace, 3, {"firms_key": "example-secret"})
    assert "example-secret" not in encrypted
    assert _open_secrets(key, workspace, 3, encrypted) == {"firms_key": "example-secret"}
    with pytest.raises(HTTPException):
        _open_secrets(key, uuid4(), 3, encrypted)
    with pytest.raises(HTTPException):
        _open_secrets(key, workspace, 4, encrypted)


def test_config_secrets_are_redacted_and_reject_invalid_values() -> None:
    request = LiveCredentialUpdate(expected_revision=1, openweather_key="example-secret")
    assert "example-secret" not in repr(request)
    for key in ("", "contains whitespace", "bad\nkey"):
        with pytest.raises(ValidationError):
            LiveCredentialUpdate(expected_revision=1, firms_key=key)


def test_missing_or_stale_terms_fail_closed() -> None:
    with pytest.raises(HTTPException) as exc:
        require_live_terms(LiveCredentials(revision=1), "usgs")
    assert exc.value.status_code == 409
    old = LiveCredentials(revision=2, terms={"usgs": {"version": "old", "declared_use": "personal"}})
    with pytest.raises(HTTPException):
        require_live_terms(old, "usgs")


@pytest.mark.asyncio
async def test_terms_accept_and_revoke_are_revision_guarded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Consent applies only after an owner save and can be withdrawn without persisting content."""
    async def bump(*args: object, **kwargs: object) -> int:
        return 2

    monkeypatch.setattr("modules.dashboard.live_credentials.workspaces.bump_configuration_revision", bump)
    row = SimpleNamespace(revision=1, encrypted_payload=None)

    class Session:
        def __init__(self) -> None:
            self.terms = []

        async def scalars(self, *args: object, **kwargs: object) -> list:
            return self.terms.copy()

        def add(self, value: object) -> None:
            self.terms.append(value)

        async def delete(self, value: object) -> None:
            self.terms.remove(value)

        async def execute(self, *args: object, **kwargs: object) -> None:
            return None

        async def scalar(self, *args: object, **kwargs: object) -> SimpleNamespace:
            return row

        async def flush(self) -> None:
            return None

    scope = WorkspaceContext(user_id=1, workspace_id=uuid4(), role="owner", membership_revision=1)
    settings = SimpleNamespace(connector_credential_encryption_key=SecretStr(Fernet.generate_key().decode()))
    kwargs = {"scope": scope, "settings": settings, "multi_workspace_enabled": False, "auth_sessions": (), "is_operator": True}
    session = Session()
    accepted = await save_live_credentials(session, LiveCredentialUpdate(
        expected_revision=1, terms_accept=["usgs"], terms_use="personal"), **kwargs)
    require_live_terms(accepted, "usgs")
    assert row.encrypted_payload is None
    assert len(session.terms) == 1
    with pytest.raises(HTTPException) as conflict:
        await save_live_credentials(session, LiveCredentialUpdate(
            expected_revision=1, terms_revoke=["usgs"]), **kwargs)
    assert conflict.value.status_code == 409
    withdrawn = await save_live_credentials(session, LiveCredentialUpdate(
        expected_revision=2, terms_revoke=["usgs"]), **kwargs)
    with pytest.raises(HTTPException):
        require_live_terms(withdrawn, "usgs")
    assert row.encrypted_payload is None


@pytest.mark.asyncio
async def test_review_class_needs_operator_and_explicit_evidence() -> None:
    scope = WorkspaceContext(user_id=2, workspace_id=uuid4(), role="owner", membership_revision=1)
    with pytest.raises(HTTPException) as denied:
        await save_live_credentials(None, LiveCredentialUpdate(
            expected_revision=1, terms_accept=["eonet"], terms_use="personal",
            terms_review_confirmed=True, terms_evidence_ref="Reviewed terms"),
            scope=scope, settings=None, multi_workspace_enabled=False, auth_sessions=(), is_operator=False)
    assert denied.value.status_code == 403


@pytest.mark.asyncio
async def test_operator_review_is_bound_to_owner_acknowledgement(monkeypatch: pytest.MonkeyPatch) -> None:
    """An old review cannot approve a replaced acknowledgement or expose its credentials."""
    from datetime import UTC, datetime, timedelta

    from modules.connectors.public import live_terms_catalog
    from modules.dashboard.live_config_routes import LiveTermsDecision, review_live_terms

    workspace = uuid4()
    now = datetime.now(UTC)
    row = SimpleNamespace(version=live_terms_catalog()["eonet"]["version"], declared_use="personal",
                          acknowledged_at=now, review_state="pending")

    class Session:
        async def scalar(self, *args: object, **kwargs: object) -> object:
            return row

        async def commit(self) -> None:
            pass

    async def bump(*args: object, **kwargs: object) -> None:
        pass

    monkeypatch.setattr("modules.dashboard.live_config_routes.bump_operator_review_revision", bump)
    monkeypatch.setattr("modules.dashboard.live_config_routes.authenticated_session_ref", lambda request: object())
    common = {"state": "approved", "evidence_ref": "reviewed", "version": row.version, "declared_use": "personal"}
    with pytest.raises(HTTPException) as stale:
        await review_live_terms(workspace, "eonet", LiveTermsDecision(**common, acknowledged_at=now - timedelta(seconds=1)),
                                object(), Session(), object())
    assert stale.value.status_code == 409
    result = await review_live_terms(workspace, "eonet", LiveTermsDecision(**common, acknowledged_at=now),
                                     object(), Session(), object())
    assert result == {"state": "approved"}
    assert row.reviewer_user_id == 1 and row.evidence_ref == "reviewed"
    with pytest.raises(HTTPException):
        await review_live_terms(workspace, "eonet", LiveTermsDecision(**common, acknowledged_at=now),
                                object(), Session(), object())


@pytest.mark.asyncio
async def test_operator_review_http_rejects_anonymous_and_other_accounts(monkeypatch: pytest.MonkeyPatch) -> None:
    """The actual HTTP dependencies gate the cross-workspace review queue and writes."""
    from datetime import UTC, datetime, timedelta

    import httpx
    from fastapi import FastAPI

    from core.auth import dependencies
    from core.database import get_session
    from modules.dashboard.live_config_routes import operator_router

    app = FastAPI()
    app.state.settings = SimpleNamespace(public_origin="http://test", multi_workspace_enabled=True, session_idle_hours=None)
    app.include_router(operator_router)
    auth = SimpleNamespace(owner_id=2, token_hash="test-hash", expires_at=datetime.now(UTC) + timedelta(hours=1))

    class Session:
        async def get(self, *args: object) -> object:
            return auth

    async def session_override():
        yield Session()

    async def active_account(*args: object, **kwargs: object) -> object:
        return object()

    app.dependency_overrides[get_session] = session_override
    monkeypatch.setattr(dependencies, "get_active_account", active_account)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/api/v1/operator/live-map/terms-review")).status_code == 401
        client.cookies.set("bbd_session", "other-account")
        assert (await client.get("/api/v1/operator/live-map/terms-review")).status_code == 401
        result = await client.put(f"/api/v1/operator/live-map/{uuid4()}/eonet/terms-review", headers={"Origin": "http://test"}, json={
            "state": "approved", "evidence_ref": "review", "version": "1", "declared_use": "personal",
            "acknowledged_at": "2026-11-04T00:00:00Z",
        })
        assert result.status_code == 401
        auth.owner_id = 1
        denied = await client.put(f"/api/v1/operator/live-map/{uuid4()}/eonet/terms-review", headers={"Origin": "http://test"}, json={
            "state": "approved", "evidence_ref": "review", "version": "1", "declared_use": "personal",
            "acknowledged_at": "2026-11-04T00:00:00Z",
        })
        assert denied.status_code == 403


@pytest.mark.asyncio
async def test_admin_server_read_selects_workspace_owner_without_exposing_contact(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.workspaces.schemas import AdminWorkspaceReadContext
    from modules.dashboard import live_config_routes, live_credentials

    scope = AdminWorkspaceReadContext(actor_user_id=1, workspace_id=uuid4(), workspace_owner_user_id=2,
                                      membership_revision=1, configuration_revision=1)
    queries = []

    class Session:
        async def scalar(self, statement):
            queries.append(statement.compile().params)

        async def scalars(self, statement):
            return []

        async def rollback(self):
            pass

    async def fence(*args, **kwargs):
        return object()

    monkeypatch.setattr(live_credentials.workspaces, 'read_access_fence', fence)
    snapshot = await live_credentials.read_live_credentials(Session(), scope=scope, settings=None, multi_workspace_enabled=True)
    assert snapshot.revision == 1
    assert 2 in queries[0].values() and 1 not in queries[0].values()
    monkeypatch.setattr(live_config_routes, 'read_access_fence', fence)
    async def credentials(*args, **kwargs):
        return live_credentials.LiveCredentials(revision=1, sec_contact_email='private@example.test')
    monkeypatch.setattr(live_config_routes, 'read_live_credentials', credentials)
    monkeypatch.setattr(live_config_routes, 'authenticated_session_ref', lambda request: object())
    monkeypatch.setattr(live_config_routes, 'PublicationFence', lambda **kwargs: object())
    monkeypatch.setattr(live_config_routes, 'require_publication_gate', lambda *args: None)
    request = SimpleNamespace(state=SimpleNamespace(auth_session=SimpleNamespace(owner_id=1)),
                              app=SimpleNamespace(state=SimpleNamespace(settings=SimpleNamespace(multi_workspace_enabled=True))))
    from fastapi import Response
    result = await live_config_routes.read_config(request, Response(), Session(), scope)
    assert result['sec_contact_email'] is None
