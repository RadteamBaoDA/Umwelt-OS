from datetime import datetime
from dataclasses import dataclass, field
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


@dataclass(frozen=True, slots=True)
class AccountSessionRef:
    """Detached exact-session locator from authentication, without a bearer or authority.

    The digest is excluded from repr. Publication must revalidate the exact live session
    under auth-owned locks; merely constructing this value proves nothing.
    """

    account_id: int
    token_hash: str = field(repr=False)

    def __post_init__(self) -> None:
        """Reject malformed actor/digest locators before short session admission."""
        if type(self.account_id) is not int or self.account_id <= 0:
            raise ValueError("A positive account ID is required")
        if not isinstance(self.token_hash, str) or len(self.token_hash) != 64 or any(
            character not in "0123456789abcdef" for character in self.token_hash
        ):
            raise ValueError("A session digest is required")


class SetupRequest(BaseModel):
    """Validated first-run owner password request."""
    password: str = Field(min_length=12, max_length=128)


class SetupStatus(BaseModel):
    """Reports whether first-run owner setup is still required."""
    setupRequired: bool


class SetupResponse(BaseModel):
    """Reports successful singleton owner creation."""
    created: bool


class LoginRequest(BaseModel):
    """Exact normalized email login; omission selects bootstrap account 1 only."""
    password: str = Field(min_length=1, max_length=128)
    identifier: str | None = Field(default=None, max_length=320)


class AuthState(BaseModel):
    """Authenticated response containing the client CSRF token."""
    authenticated: bool = True
    csrfToken: str


class CsrfResponse(BaseModel):
    """Response carrying a newly issued client CSRF token."""
    csrfToken: str


class AccountRead(BaseModel):
    """Detached active account identity; omits password hashes and linked provider subjects.

    Bootstrap email may be unset. Invitation bearer authority does not establish mailbox
    verification; only verified Google OIDC provenance is represented in this pilot.
    """

    model_config = ConfigDict(frozen=True)
    id: int
    email: str | None
    account_state: Literal["active"] = "active"
    default_workspace_id: UUID
    email_verified_at: datetime | None
    email_verification_source: Literal["google_oidc"] | None
