"""authenticated_session_ref must not read the live auth ORM row once require_account captured its ids."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from core.auth.models import AuthSession
from core.auth.public import authenticated_session_ref
from core.auth.schemas import AccountRead


def _request(row: AuthSession, ids: tuple[int, str] | None, account_id: int = 1) -> SimpleNamespace:
    account = AccountRead(id=account_id, email=None, default_workspace_id=uuid4(),
                          email_verified_at=None, email_verification_source=None)
    state = SimpleNamespace(auth_session=row, account=account)
    if ids is not None:
        state.auth_session_ids = ids
    return SimpleNamespace(state=state)


def test_captured_ids_win_over_the_live_row() -> None:
    """A rolled-back row would lazy-load (MissingGreenlet); the captured ids are used instead."""
    live = AuthSession(token_hash="b" * 64, owner_id=999)  # stands in for values that must not be read
    ref = authenticated_session_ref(_request(live, (1, "a" * 64)))  # type: ignore[arg-type]
    assert ref.account_id == 1 and ref.token_hash == "a" * 64


def test_falls_back_to_the_live_row_without_captured_ids() -> None:
    ref = authenticated_session_ref(_request(AuthSession(token_hash="c" * 64, owner_id=1), None))  # type: ignore[arg-type]
    assert ref.token_hash == "c" * 64


def test_mismatched_captured_account_is_rejected() -> None:
    with pytest.raises(HTTPException) as caught:
        authenticated_session_ref(_request(AuthSession(token_hash="a" * 64, owner_id=1), (2, "a" * 64)))  # type: ignore[arg-type]
    assert caught.value.status_code == 401
