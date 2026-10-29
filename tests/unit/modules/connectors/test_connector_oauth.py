"""Mock-only OAuth binding, PKCE and one-time callback checks for connector grants."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from starlette.requests import Request

from modules.connectors import oauth_routes, reddit_oauth_routes


def test_oauth_ciphertext_is_bound_to_source_operation_and_revision():
    key = Fernet.generate_key().decode("ascii")
    source_id, operation_id = uuid4(), uuid4()
    encrypted = oauth_routes._cipher(key, source_id, operation_id, 3, 7, {"refresh_token": "secret"})
    assert oauth_routes._open_cipher(key, encrypted, source_id, operation_id, 3, 7) == {"refresh_token": "secret"}
    with pytest.raises(ValueError, match="binding"):
        oauth_routes._open_cipher(key, encrypted, source_id, operation_id, 3, 8)


def test_authorization_url_uses_pkce_and_provider_read_only_scope():
    settings = SimpleNamespace(connector_google_client_id="client-id",
                               connector_google_callback_url="https://os.example/api/v1/connectors/oauth/google/callback")
    state, verifier = "one-time-state", "high-entropy-verifier"
    parsed = parse_qs(urlsplit(oauth_routes._authorize_url(settings, "google_mail", state, verifier)).query)
    assert parsed["state"] == [state]
    assert parsed["code_challenge_method"] == ["S256"]
    assert parsed["scope"] == [oauth_routes._PROVIDER_SCOPES["google_mail"]]
    assert "gmail.readonly" in parsed["scope"][0]
    assert "gmail.modify" not in parsed["scope"][0]


@pytest.mark.asyncio
async def test_google_callback_consumes_state_once_and_rejects_replay(monkeypatch: pytest.MonkeyPatch):
    state = "single-use-state"
    source_id, operation_id, workspace_id = uuid4(), uuid4(), uuid4()
    owner = SimpleNamespace(token_hash="session-hash", owner_id=5)
    scope = SimpleNamespace(role="owner", workspace_id=workspace_id, user_id=5)
    source = SimpleNamespace(provider="google_mail", generation=3)
    row = SimpleNamespace(desired_revision=7, credential_revision=2)
    attempt = SimpleNamespace(
        state_hash=oauth_routes._digest(state), consumed_at=None, expires_at=datetime.now(UTC) + timedelta(minutes=2),
        session_hash=owner.token_hash, owner_id=owner.owner_id, workspace_id=workspace_id,
        provider="google_mail", source_id=source_id, id=operation_id, source_generation=3,
        configuration_revision=7, credential_revision=2, expected_token_revision=0,
        encrypted_verifier="sealed-verifier",
    )
    settings = SimpleNamespace(
        connector_google_callback_url="https://os.example/api/v1/connectors/oauth/google/callback",
        public_origin="https://os.example", connector_credential_encryption_key=SimpleNamespace(get_secret_value=lambda: "key"),
        multi_workspace_enabled=False,
    )
    app = SimpleNamespace(state=SimpleNamespace(settings=settings))
    request = Request({
        "type": "http", "method": "GET", "scheme": "https", "path": oauth_routes._CALLBACK_PATH,
        "raw_path": oauth_routes._CALLBACK_PATH.encode(), "query_string": urlencode({"state": state, "code": "code"}).encode(),
        "headers": [(b"host", b"os.example")], "server": ("os.example", 443), "client": ("127.0.0.1", 1),
        "app": app,
    })
    fence = object()
    monkeypatch.setattr(oauth_routes, "_configured", lambda _settings: None)
    monkeypatch.setattr(oauth_routes, "_admit", AsyncMock(return_value=fence))
    monkeypatch.setattr(oauth_routes, "_current_source", AsyncMock(return_value=(source, row)))
    monkeypatch.setattr(oauth_routes, "_require_current_session", AsyncMock())
    monkeypatch.setattr(oauth_routes, "_open_cipher", lambda *_args: {"verifier": "pkce-verifier"})
    exchange = AsyncMock(return_value={
        "access_token": "access", "refresh_token": "refresh", "access_expires_at": datetime.now(UTC).isoformat(),
        "scopes": [oauth_routes._PROVIDER_SCOPES["google_mail"]],
    })
    monkeypatch.setattr(oauth_routes, "_exchange", exchange)
    monkeypatch.setattr(oauth_routes, "_cipher", lambda *_args: "encrypted-tokens")
    publish = AsyncMock()
    monkeypatch.setattr(oauth_routes, "_publish", publish)

    session = SimpleNamespace(scalar=AsyncMock(side_effect=[attempt, None]), rollback=AsyncMock(), add=MagicMock())
    response = await oauth_routes.complete_google_authorization(request, session, owner, scope)
    assert response.status_code == 303 and "oauth=connected" in response.headers["location"]
    assert attempt.consumed_at is not None
    assert session.add.call_count == 1
    assert publish.await_count == 2
    exchange.assert_awaited_once_with(settings, "code", state, "pkce-verifier", "google_mail")

    replay_session = SimpleNamespace(scalar=AsyncMock(return_value=attempt), rollback=AsyncMock(), add=MagicMock())
    with pytest.raises(HTTPException) as rejected:
        await oauth_routes.complete_google_authorization(request, replay_session, owner, scope)
    assert rejected.value.status_code == 400
    exchange.assert_awaited_once()


@pytest.mark.asyncio
async def test_reddit_callback_consumes_state_once_and_rejects_replay(monkeypatch: pytest.MonkeyPatch):
    state = "reddit-single-use-state"
    source_id, operation_id, workspace_id = uuid4(), uuid4(), uuid4()
    owner = SimpleNamespace(token_hash="session-hash", owner_id=5)
    scope = SimpleNamespace(role="owner", workspace_id=workspace_id, user_id=5)
    source = SimpleNamespace(provider="reddit", generation=3)
    row = SimpleNamespace(desired_revision=7, credential_revision=2)
    attempt = SimpleNamespace(
        state_hash=reddit_oauth_routes._digest(state), consumed_at=None,
        expires_at=datetime.now(UTC) + timedelta(minutes=2), session_hash=owner.token_hash,
        owner_id=owner.owner_id, workspace_id=workspace_id, provider="reddit", source_id=source_id,
        id=operation_id, source_generation=3, configuration_revision=7, credential_revision=2,
        expected_token_revision=0, encrypted_verifier="unused-reddit-marker",
    )
    key = Fernet.generate_key().decode("ascii")
    settings = SimpleNamespace(
        connector_reddit_callback_url="https://os.example/api/v1/connectors/oauth/reddit/callback",
        public_origin="https://os.example", connector_reddit_client_id="client-id",
        connector_reddit_client_secret=SimpleNamespace(get_secret_value=lambda: "client-secret"),
        connector_credential_encryption_key=SimpleNamespace(get_secret_value=lambda: key),
        multi_workspace_enabled=False, is_dev=False,
    )
    app = SimpleNamespace(state=SimpleNamespace(settings=settings))
    query = urlencode({"state": state, "code": "authorization-code"})
    path = reddit_oauth_routes._CALLBACK_PATH
    request = Request({
        "type": "http", "method": "GET", "scheme": "https", "path": path,
        "raw_path": path.encode(), "query_string": query.encode(),
        "headers": [(b"host", b"os.example")], "server": ("os.example", 443),
        "client": ("127.0.0.1", 1), "app": app,
    })
    fence = object()
    monkeypatch.setattr(reddit_oauth_routes, "_configured", lambda _settings: None)
    monkeypatch.setattr(reddit_oauth_routes, "_admit", AsyncMock(return_value=fence))
    monkeypatch.setattr(reddit_oauth_routes, "_current_source", AsyncMock(return_value=(source, row)))
    monkeypatch.setattr(reddit_oauth_routes, "_require_session", AsyncMock())
    exchange = AsyncMock(return_value={
        "access_token": "access", "refresh_token": "refresh",
        "access_expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(), "scopes": ["read"],
    })
    monkeypatch.setattr(reddit_oauth_routes, "_exchange", exchange)
    publish = AsyncMock()
    monkeypatch.setattr(reddit_oauth_routes, "_publish", publish)
    session = SimpleNamespace(
        scalar=AsyncMock(side_effect=[attempt, None]), rollback=AsyncMock(), add=MagicMock(),
    )

    response = await reddit_oauth_routes.complete_reddit_authorization(request, session, owner, scope)
    assert response.status_code == 303 and "oauth=reddit_connected" in response.headers["location"]
    assert attempt.consumed_at is not None
    assert session.add.call_count == 1
    grant = session.add.call_args.args[0]
    assert grant.provider == "reddit" and grant.granted_scopes == ["read"]
    exchange.assert_awaited_once_with(settings, "authorization-code")

    replay_session = SimpleNamespace(scalar=AsyncMock(return_value=attempt), rollback=AsyncMock(), add=MagicMock())
    with pytest.raises(HTTPException) as rejected:
        await reddit_oauth_routes.complete_reddit_authorization(request, replay_session, owner, scope)
    assert rejected.value.status_code == 400
    exchange.assert_awaited_once()


@pytest.mark.asyncio
async def test_google_collection_reads_the_locked_grant_and_scope(monkeypatch: pytest.MonkeyPatch):
    from contextlib import asynccontextmanager

    from modules.connectors import collection
    from modules.ingestion import public as ingestion

    source_id, workspace_id, operation_id = uuid4(), uuid4(), uuid4()
    source_fence, access_fence = object(), object()
    source = SimpleNamespace(id=source_id, generation=3, provider="google_mail")
    scope = SimpleNamespace(workspace_id=workspace_id, actor_user_id=5)
    attempt = SimpleNamespace(
        source=source, scope=scope, multi=False, access_fence=access_fence, source_fence=source_fence,
        credential_revision=2, connector_revision=7,
    )
    row = SimpleNamespace(credential_revision=2)
    grant = SimpleNamespace(
        state="ready", provider="google_mail", workspace_id=workspace_id, owner_id=5,
        source_generation=3, configuration_revision=7, credential_revision=2,
        encrypted_tokens="sealed", granted_scopes=[oauth_routes._PROVIDER_SCOPES["google_mail"]],
        token_expires_at=datetime.now(UTC) + timedelta(hours=1),
        operation_id=operation_id, token_revision=4,
    )
    session = SimpleNamespace(scalar=AsyncMock(return_value=grant), rollback=AsyncMock())

    @asynccontextmanager
    async def factory():
        yield session

    settings = SimpleNamespace(connector_credential_encryption_key=SimpleNamespace(get_secret_value=lambda: "key"))
    run = SimpleNamespace(attempt=attempt, factory=factory, settings=settings)
    monkeypatch.setattr(collection.provisioning, "lock_connector", AsyncMock(return_value=(source_fence, row, ())))
    monkeypatch.setattr(collection.provisioning, "require_collection_fence", AsyncMock(return_value=True))
    monkeypatch.setattr(ingestion, "lock_source_credentials_in_uow", AsyncMock())
    monkeypatch.setattr(oauth_routes, "_open_cipher", lambda *_args: {"access_token": "access", "refresh_token": "refresh"})

    access, binding = await collection._google_access_token(run)

    assert access == "access" and binding == (operation_id, 4)
    assert session.scalar.await_count == 1
    assert session.rollback.await_count == 1


@pytest.mark.parametrize("provider", ["google_mail", "reddit"])
@pytest.mark.asyncio
async def test_oauth_collection_receipt_rechecks_the_locked_owner_grant(
    monkeypatch: pytest.MonkeyPatch, provider: str,
):
    from contextlib import asynccontextmanager

    from modules.connectors import collection
    from modules.ingestion import public as ingestion

    source_id, workspace_id, operation_id = uuid4(), uuid4(), uuid4()
    source_fence, access_fence = object(), object()
    scope = SimpleNamespace(workspace_id=workspace_id, actor_user_id=5)
    source = SimpleNamespace(id=source_id, generation=3, provider=provider)
    attempt = SimpleNamespace(
        source=source, scope=scope, multi=False, access_fence=access_fence, source_fence=source_fence,
        credential_revision=2, connector_revision=7, ref=object(),
    )
    lease = SimpleNamespace(source_generation=3, connector_revision=7, token=uuid4())
    run = SimpleNamespace(attempt=attempt, extras={"lease": lease})
    row = SimpleNamespace(credential_revision=2)
    grant = SimpleNamespace(
        provider=provider, state="ready", workspace_id=workspace_id, owner_id=5,
        credential_revision=2, operation_id=operation_id, token_revision=4,
        source_generation=3, configuration_revision=7,
    )
    session = SimpleNamespace(scalar=AsyncMock(return_value=grant), rollback=AsyncMock())

    @asynccontextmanager
    async def factory():
        yield session

    run.factory = factory
    monkeypatch.setattr(collection.provisioning, "lock_connector", AsyncMock(return_value=(source_fence, row, ())))
    monkeypatch.setattr(collection.provisioning, "require_collection_fence", AsyncMock(return_value=True))
    monkeypatch.setattr(ingestion, "lock_source_credentials_in_uow", AsyncMock())
    validate = AsyncMock(return_value=True)
    monkeypatch.setattr(ingestion, "validate_connector_collection_in_uow", validate)

    binding = (operation_id, 4)
    if provider == "google_mail":
        await collection._credential_fence(run, google=binding)
    else:
        await collection._credential_fence(run, reddit=binding)

    assert session.scalar.await_count == 1
    validate.assert_awaited_once()
    assert session.rollback.await_count == 1


@pytest.mark.asyncio
async def test_google_disconnect_fences_owner_and_revokes_the_saved_grant(monkeypatch: pytest.MonkeyPatch):
    source_id, workspace_id, operation_id = uuid4(), uuid4(), uuid4()
    owner = SimpleNamespace(token_hash="session-hash", owner_id=5)
    scope = SimpleNamespace(role="owner", workspace_id=workspace_id, user_id=5)
    source = SimpleNamespace(provider="google_mail", status="active", generation=3)
    row = SimpleNamespace(desired_revision=7, credential_revision=2)
    grant = SimpleNamespace(
        provider="google_mail", workspace_id=workspace_id, owner_id=5, token_revision=4,
        encrypted_tokens="sealed", operation_id=operation_id, source_generation=3,
        configuration_revision=7, credential_revision=2, state="ready", error_code=None,
        granted_scopes=[oauth_routes._PROVIDER_SCOPES["google_mail"]], token_expires_at=datetime.now(UTC),
    )
    settings = SimpleNamespace(
        connector_credential_encryption_key=SimpleNamespace(get_secret_value=lambda: "key"),
        multi_workspace_enabled=False,
    )
    request = Request({
        "type": "http", "method": "POST", "scheme": "https", "path": "/disconnect",
        "raw_path": b"/disconnect", "query_string": b"", "headers": [(b"host", b"os.example")],
        "server": ("os.example", 443), "client": ("127.0.0.1", 1),
        "app": SimpleNamespace(state=SimpleNamespace(settings=settings)),
    })

    class RevocationClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            return SimpleNamespace(status_code=200, raise_for_status=lambda: None)

    session = SimpleNamespace(scalar=AsyncMock(side_effect=[grant, grant]))
    monkeypatch.setattr(oauth_routes, "_admit", AsyncMock(return_value=object()))
    monkeypatch.setattr(oauth_routes, "_current_source", AsyncMock(return_value=(source, row)))
    monkeypatch.setattr(oauth_routes, "_open_cipher", lambda *_args: {"refresh_token": "refresh"})
    monkeypatch.setattr(oauth_routes, "_publish", AsyncMock())
    monkeypatch.setattr(oauth_routes, "_require_current_session", AsyncMock())
    monkeypatch.setattr(oauth_routes.httpx, "AsyncClient", lambda **_kwargs: RevocationClient())

    result = await oauth_routes.disconnect_google_source(
        str(source_id), oauth_routes.OAuthDisconnectRequest(
            expected_source_generation=3, expected_revision=7, expected_token_revision=4),
        session, request, owner, scope,
    )

    assert result == {"state": "revoked"}
    assert row.credential_revision == grant.credential_revision == 3
    assert grant.state == "revoked" and grant.encrypted_tokens is None and grant.granted_scopes == []


def test_configuration_read_accepts_oauth2_auth_method():
    """Google/Reddit snapshots report `oauth2`; the response model must serialize it instead of answering 500."""
    from modules.connectors.provisioning_routes import ConnectorConfigurationRead

    read = ConnectorConfigurationRead(
        workspace_id=uuid4(), source_id=uuid4(), source_type="api", source_generation=1, configuration={},
        expected_revision=0, auth_method="oauth2", auth_header_name=None, desired_enabled=False,
        activation_state="saved_not_active", activation_error_code=None,
        provider_credential_configured=False, provider_credential_state="not_connected",
    )
    assert read.auth_method == "oauth2"
