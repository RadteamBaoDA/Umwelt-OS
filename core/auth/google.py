import httpx
from authlib.integrations.starlette_client import OAuth  # type: ignore[import-untyped]  # no stubs

from core.config import Settings

GOOGLE_ISSUER = "https://accounts.google.com"
GOOGLE_METADATA_URL = f"{GOOGLE_ISSUER}/.well-known/openid-configuration"
GOOGLE_CALLBACK_PATH = "/api/v1/auth/google/callback"


def google_client(settings: Settings) -> OAuth:
    """Configure the Google OpenID Connect client with bounded HTTP timeouts and PKCE."""
    oauth = OAuth()
    oauth.register(
        name="google",
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret.get_secret_value(),
        server_metadata_url=GOOGLE_METADATA_URL,
        client_kwargs={
            "scope": "openid email profile",
            "code_challenge_method": "S256",
            "timeout": httpx.Timeout(4, connect=2),
        },
    )
    return oauth
