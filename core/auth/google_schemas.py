from pydantic import BaseModel, Field, SecretStr


class GoogleStartRequest(BaseModel):
    """Login/link or invitation-bound password enrollment; bearer never enters OAuth state."""
    purpose: str = Field(pattern="^(login|link|invitation)$")
    invitation_token: SecretStr | None = Field(default=None, min_length=40, max_length=256)


class GoogleStatus(BaseModel):
    """Reports Google OAuth configuration and current link state."""
    configured: bool
    linked: bool


class GoogleStartResponse(BaseModel):
    """Returns the Google authorization URL for the initiated flow."""
    authorization_url: str


class ReauthenticateRequest(BaseModel):
    """Validated password request for recent-authentication confirmation."""
    password: str = Field(min_length=1, max_length=128)
