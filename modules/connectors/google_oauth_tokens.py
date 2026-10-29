"""Server-side token refresh for read-only Google connector grants."""

# Boundary validation uses ValueError for malformed provider/token data and stable failure mapping.
# ruff: noqa: TRY004

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from authlib.integrations.httpx_client import AsyncOAuth2Client  # type: ignore[import-untyped]

from core.config import Settings
from modules.connectors.oauth_routes import _PROVIDER_SCOPES, _TOKEN_URL


async def refresh_google_token(settings: Settings, provider: str, refresh_token: str) -> dict[str, Any]:
    """Exchange one stored refresh token at Google's fixed HTTPS token endpoint.

    The caller owns grant locking, source/revision fencing, encryption, and persistence. This
    helper sends no browser input, follows no redirects, avoids ambient proxies, accepts only
    the provider's existing read-only scope, and never includes response bodies in errors.
    """
    scope = _PROVIDER_SCOPES.get(provider)
    if scope is None or not refresh_token or len(refresh_token) > 8192:
        raise ValueError("google_refresh_request_invalid")
    try:
        async with AsyncOAuth2Client(
            client_id=settings.connector_google_client_id,
            client_secret=settings.connector_google_client_secret.get_secret_value(),
            timeout=httpx.Timeout(10), follow_redirects=False, trust_env=False,
        ) as client:
            payload = await client.refresh_token(_TOKEN_URL, refresh_token=refresh_token)
    except Exception as exc:  # noqa: BLE001  # OAuth errors may include URL/request details; expose only a stable code.
        del exc
        raise ValueError("google_refresh_unavailable") from None
    if not isinstance(payload, dict):
        raise ValueError("google_refresh_response_invalid")
    access_token = payload.get("access_token")
    expires_in = payload.get("expires_in")
    token_type = payload.get("token_type", "Bearer")
    granted = payload.get("scope")
    if (
        not isinstance(access_token, str) or not access_token or len(access_token) > 8192
        or isinstance(expires_in, bool) or not isinstance(expires_in, (int, float))
        or not 0 < expires_in <= 7200 or str(token_type).casefold() != "bearer"
        or granted is not None and (not isinstance(granted, str) or scope not in granted.split())
    ):
        raise ValueError("google_refresh_response_invalid")
    rotated = payload.get("refresh_token", refresh_token)
    if not isinstance(rotated, str) or not rotated or len(rotated) > 8192:
        raise ValueError("google_refresh_response_invalid")
    return {
        "access_token": access_token,
        "refresh_token": rotated,
        "access_expires_at": (datetime.now(UTC) + timedelta(seconds=expires_in)).isoformat(),
    }
