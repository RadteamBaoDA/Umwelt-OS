from pydantic import BaseModel, Field


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
    """Validated password login request."""
    password: str = Field(min_length=1, max_length=128)


class AuthState(BaseModel):
    """Authenticated response containing the client CSRF token."""
    authenticated: bool = True
    csrfToken: str


class CsrfResponse(BaseModel):
    """Response carrying a newly issued client CSRF token."""
    csrfToken: str


class ChangePasswordRequest(BaseModel):
    """Current password plus a replacement validated with the same policy as first-run setup."""
    currentPassword: str = Field(min_length=1, max_length=128)
    newPassword: str = Field(min_length=12, max_length=128)
