"""Characterization tests for the Google OIDC callback (invitation, login and link branches).

They drive ``core.auth.routes.google_callback`` end to end with every collaborator replaced by a
spy, so they pin redirects, cookies and the commit/rollback order independent of how the handler is
factored into helpers.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from core.auth import routes
from core.auth.google import GOOGLE_ISSUER

SUB = "google-sub-1"
EMAIL = "owner@example.com"
BINDING = "binding-cookie"


class _Secret:
    """Minimal SecretStr stand-in."""

    def get_secret_value(self) -> str:
        """Return a non-empty client secret."""
        return "secret"


class _Harness:
    """Wires google_callback to spies and records ordered session/Redis calls."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, purpose: str, **transaction: Any) -> None:
        """Build request, session, redis and patch all collaborators for ``purpose``."""
        self.calls: list[str] = []
        self.settings = SimpleNamespace(
            multi_workspace_enabled=True,
            secure_cookies=False,
            session_lifetime_hours=1,
            google_client_id="id",
            google_client_secret=_Secret(),
        )
        self.cookies = {
            "bbd_google_binding": BINDING,
            routes.CSRF_COOKIE: "csrf-cookie",
            routes.SESSION_COOKIE: "sess-cookie",
        }
        self.transaction = {
            "purpose": purpose,
            "binding": routes._hash(BINDING),
            **transaction,
        }
        self.request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(settings=self.settings)),
            query_params={"state": "st"},
            cookies=self.cookies,
            headers={},
            client=None,
        )
        self.redis = MagicMock()
        self.redis.getdel = AsyncMock(return_value=routes.json.dumps(self.transaction))
        self.redis.set = AsyncMock(return_value=True)
        self.session = MagicMock()
        self.session.scalar = AsyncMock(return_value=None)
        self.session.add = MagicMock(
            side_effect=lambda obj: self.calls.append(f"add:{type(obj).__name__}")
        )
        self.session.delete = AsyncMock(side_effect=lambda obj: self.calls.append("delete"))
        self.session.commit = AsyncMock(side_effect=lambda: self.calls.append("commit"))
        self.session.rollback = AsyncMock(side_effect=lambda: self.calls.append("rollback"))
        self.userinfo = {"iss": GOOGLE_ISSUER, "sub": SUB, "email": EMAIL, "email_verified": True}
        client = SimpleNamespace(
            google=SimpleNamespace(
                authorize_access_token=AsyncMock(
                    side_effect=lambda req: {"userinfo": self.userinfo}
                ),
            )
        )
        monkeypatch.setattr(routes, "google_client", lambda settings: client)
        monkeypatch.setattr(routes, "_new_csrf", lambda settings: ("csrf-token", "csrf-cookie-new"))
        self.audit = MagicMock()
        monkeypatch.setattr(routes, "_audit", self.audit)
        self.active = AsyncMock(return_value=object())
        monkeypatch.setattr(routes, "get_active_account", self.active)
        self.monkeypatch = monkeypatch

    async def run(self) -> Any:
        """Invoke the route handler."""
        return await routes.google_callback(self.request, self.session, self.redis)

    def set_cookie_names(self, response: Any) -> list[str]:
        """Return the names of cookies set by ``response``."""
        return [h.split(b"=", 1)[0].decode() for k, h in response.raw_headers if k == b"set-cookie"]


def _owner(owner_id: int = 5, email: str = EMAIL) -> SimpleNamespace:
    """Build a fake locked owner row."""
    return SimpleNamespace(
        id=owner_id, email=email, email_verified_at=None, email_verification_source=None
    )


# --- invitation branch -----------------------------------------------------------------------


def _invitation(
    monkeypatch: pytest.MonkeyPatch, target_email: str = EMAIL, **extra: Any
) -> _Harness:
    """Harness for an invitation callback with a matching CSRF cookie hash."""
    h = _Harness(
        monkeypatch,
        "invitation",
        invitation_hash="inv-hash",
        csrf_cookie_hash=routes._hash("csrf-cookie"),
        **extra,
    )

    async def target(session: Any, digest: str) -> Any:
        """Resolve the invitation target."""
        assert digest == "inv-hash"
        return SimpleNamespace(email=target_email)

    h.monkeypatch.setattr("core.workspaces.public.invitation_target", target)
    return h


@pytest.mark.asyncio
async def test_invitation_csrf_hash_mismatch_returns_error_without_redis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A CSRF cookie that does not match the transaction hash never reaches Redis or SQL."""
    h = _invitation(monkeypatch)
    h.cookies[routes.CSRF_COOKIE] = "other"
    response = await h.run()
    assert response.headers["location"] == "/login?google=error"
    h.redis.set.assert_not_called()
    assert h.calls == []


@pytest.mark.asyncio
async def test_invitation_target_http_error_rolls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """invitation_target raising HTTPException rolls back and returns the error redirect."""
    h = _invitation(monkeypatch)

    async def boom(session: Any, digest: str) -> Any:
        """Reject the invitation."""
        raise HTTPException(status_code=410)

    monkeypatch.setattr("core.workspaces.public.invitation_target", boom)
    response = await h.run()
    assert response.headers["location"] == "/login?google=error"
    assert h.calls == ["rollback"]
    h.redis.set.assert_not_called()


@pytest.mark.asyncio
async def test_invitation_email_mismatch_rolls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """An invitation for another mailbox rolls back and never mints a Redis proof."""
    h = _invitation(monkeypatch, target_email="someone@else.com")
    response = await h.run()
    assert response.headers["location"] == "/login?google=error"
    assert h.calls == ["rollback"]
    h.redis.set.assert_not_called()


@pytest.mark.asyncio
async def test_invitation_success_sets_enrollment_cookie_after_rollback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Success rolls back reads, stores a 5-minute NX proof and redirects to the invitation page."""
    h = _invitation(monkeypatch)
    response = await h.run()
    assert response.headers["location"] == "/invitations/accept?google=enrollment"
    assert h.calls == ["rollback"]
    args, kwargs = h.redis.set.call_args
    assert args[0].startswith("auth:google:enrollment:")
    assert kwargs == {"ex": 300, "nx": True}
    proof = routes.json.loads(args[1])
    assert proof == {
        "invitation_hash": "inv-hash",
        "email": EMAIL,
        "issuer": GOOGLE_ISSUER,
        "subject": SUB,
        "csrf_cookie_hash": routes._hash("csrf-cookie"),
    }
    assert "bbd_google_enrollment" in h.set_cookie_names(response)
    h.audit.assert_not_called()


# --- login branch ----------------------------------------------------------------------------


def _login(
    monkeypatch: pytest.MonkeyPatch, owner: Any, identity: Any, owner_id: int | None = 5
) -> _Harness:
    """Harness for a login callback resolving ``owner``/``identity``."""
    h = _Harness(monkeypatch, "login")
    h.session.scalar = AsyncMock(return_value=owner_id)
    monkeypatch.setattr(routes, "_lock_owner", AsyncMock(return_value=owner))
    monkeypatch.setattr(routes, "_lock_identity_for_subject", AsyncMock(return_value=identity))
    return h


@pytest.mark.asyncio
async def test_login_unlinked_subject_rolls_back_to_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unlinked Google subject rolls back and redirects to the login error."""
    h = _login(monkeypatch, None, None, owner_id=None)
    response = await h.run()
    assert response.headers["location"] == "/login?google=error"
    assert h.calls == ["rollback"]


@pytest.mark.asyncio
async def test_login_inactive_account_rolls_back_to_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """An inactive account rolls back and redirects to the login error."""
    h = _login(monkeypatch, _owner(), SimpleNamespace(owner_id=5))
    h.active.return_value = None
    response = await h.run()
    assert response.headers["location"] == "/login?google=error"
    assert h.calls == ["rollback"]


@pytest.mark.asyncio
async def test_login_success_issues_session_without_reauthentication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Success commits a new AuthSession with reauthenticated_at=None and redirects to /app."""
    h = _login(monkeypatch, _owner(), SimpleNamespace(owner_id=5))
    response = await h.run()
    assert response.headers["location"] == "/app"
    assert h.calls == ["add:AuthSession", "commit"]
    created = h.session.add.call_args.args[0]
    assert created.owner_id == 5 and created.reauthenticated_at is None
    assert {"bbd_session", "bbd_csrf"} <= set(h.set_cookie_names(response))
    h.audit.assert_called_once_with(h.request, "google_login", 5)


@pytest.mark.asyncio
async def test_login_integrity_error_rolls_back_to_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A commit-time IntegrityError rolls back and redirects to the login error without cookies."""
    h = _login(monkeypatch, _owner(), SimpleNamespace(owner_id=5))
    h.session.commit = AsyncMock(side_effect=IntegrityError("s", {}, Exception()))
    response = await h.run()
    assert response.headers["location"] == "/login?google=error"
    h.session.rollback.assert_awaited_once()
    assert "bbd_session" not in h.set_cookie_names(response)


# --- link branch -----------------------------------------------------------------------------


def _link(
    monkeypatch: pytest.MonkeyPatch, auth: Any = None, owner: Any = None, **tx: Any
) -> _Harness:
    """Harness for a link callback with a locked owner, auth session and no prior identity."""
    h = _Harness(monkeypatch, "link", owner_id=5, session_hash="tok-hash", **tx)
    h.session.scalar = AsyncMock(return_value=None)
    monkeypatch.setattr(routes, "_lock_owner", AsyncMock(return_value=owner or _owner()))
    monkeypatch.setattr(routes, "_lock_auth_session", AsyncMock(return_value=auth))
    monkeypatch.setattr(routes, "_lock_identity_for_owner", AsyncMock(return_value=None))
    monkeypatch.setattr(routes, "_lock_identity_for_subject", AsyncMock(return_value=None))
    monkeypatch.setattr(routes, "_require_recent_reauthentication", lambda a: None)
    return h


def _auth(token_hash: str = "tok-hash") -> SimpleNamespace:
    """Build a fake locked auth session."""
    return SimpleNamespace(
        token_hash=token_hash, reauthenticated_at=datetime.now(UTC) - timedelta(minutes=1)
    )


@pytest.mark.asyncio
async def test_link_non_int_owner_id_returns_without_rollback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed owner id returns the error redirect before any lock, so nothing is rolled back."""
    h = _link(monkeypatch, _auth())
    h.transaction["owner_id"] = True
    h.redis.getdel = AsyncMock(return_value=routes.json.dumps(h.transaction))
    response = await h.run()
    assert response.headers["location"] == "/knowledge/documents?google=error"
    assert h.calls == []


@pytest.mark.asyncio
async def test_link_session_hash_mismatch_rolls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """A different auth session than the one that started the link rolls back."""
    h = _link(monkeypatch, _auth("other-hash"))
    response = await h.run()
    assert response.headers["location"] == "/knowledge/documents?google=error"
    assert h.calls == ["rollback"]


@pytest.mark.asyncio
async def test_link_requires_recent_reauthentication(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stale password reauthentication (HTTPException) rolls back to the link error page."""
    h = _link(monkeypatch, _auth())

    def stale(auth: Any) -> None:
        """Reject the session as not recently reauthenticated."""
        raise HTTPException(status_code=403)

    monkeypatch.setattr(routes, "_require_recent_reauthentication", stale)
    response = await h.run()
    assert response.headers["location"] == "/knowledge/documents?google=error"
    assert h.calls == ["rollback"]
    assert "bbd_session" not in h.set_cookie_names(response)


@pytest.mark.asyncio
async def test_link_success_replaces_session_and_verifies_matching_email(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Success deletes the old session, links the identity, verifies the mailbox and carries reauth."""
    auth = _auth()
    owner = _owner()
    h = _link(monkeypatch, auth, owner)
    response = await h.run()
    assert response.headers["location"] == "/knowledge/documents?google=linked"
    assert h.calls == ["add:GoogleIdentity", "delete", "add:AuthSession", "commit"]
    new_session = h.session.add.call_args.args[0]
    assert new_session.reauthenticated_at == auth.reauthenticated_at
    assert owner.email_verified_at is not None and owner.email_verification_source == "google_oidc"
    h.audit.assert_called_once_with(h.request, "google_link", 5)


@pytest.mark.asyncio
async def test_link_does_not_verify_mismatched_email(monkeypatch: pytest.MonkeyPatch) -> None:
    """Email verification evidence is applied only when the owner mailbox equals the Google email."""
    owner = _owner(email="different@example.com")
    h = _link(monkeypatch, _auth(), owner)
    response = await h.run()
    assert response.headers["location"] == "/knowledge/documents?google=linked"
    assert owner.email_verified_at is None and owner.email_verification_source is None
