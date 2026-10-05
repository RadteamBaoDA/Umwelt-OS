"""Server-owned GitHub App expiring user OAuth, encrypted grant, and fixed-host calls."""

from datetime import UTC, datetime, timedelta
import hashlib
import json
import secrets
from typing import Any, Awaitable, Callable
from uuid import UUID

import httpx
from authlib.integrations.httpx_client import AsyncOAuth2Client
from cryptography.fernet import Fernet, InvalidToken

from core.config import Settings
from modules.connectors.credentials import CredentialEncryptionUnavailable
from modules.connectors.github.schemas import GitHubSourceConfig

AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
TOKEN_URL = "https://github.com/login/oauth/access_token"
REVOKE_URL = "https://api.github.com/applications/{client_id}/grant"
CALLBACK_PATH = "/api/v1/connectors/github/oauth/callback"


def _positive_provider_id(value: object) -> bool:
    """Accept only a positive, nonboolean signed-64-bit GitHub identity integer."""
    return isinstance(value, int) and not isinstance(value, bool) and 0 < value <= 2**63 - 1


def _token_cipher(key: str, source_id: UUID, operation_id: UUID, generation: int, revision: int, value: dict[str, Any]) -> str:
    """Encrypt provider credentials with source, operation, and configuration fence binding."""
    payload = {"source_id": str(source_id), "operation_id": str(operation_id), "generation": generation, "revision": revision, "tokens": value}
    def serialize_value(item: Any) -> str:
        """Encode supported timestamp fields without silently accepting other objects."""
        if isinstance(item, datetime):
            return item.isoformat()
        raise TypeError("Unsupported token value")

    return Fernet(key.encode("ascii")).encrypt(json.dumps(
        payload, sort_keys=True, separators=(",", ":"),
        default=serialize_value,
    ).encode()).decode("ascii")


def _open_token_cipher(key: str, ciphertext: str, source_id: UUID, operation_id: UUID, generation: int, revision: int) -> dict[str, Any]:
    """Decrypt a GitHub token pair only when its exact local source and revision match."""
    try:
        payload = json.loads(Fernet(key.encode("ascii")).decrypt(ciphertext.encode("ascii")))
    except (ValueError, UnicodeEncodeError, InvalidToken, json.JSONDecodeError) as exc:
        raise CredentialEncryptionUnavailable("Stored GitHub grant is unavailable") from exc
    if (payload.get("source_id"), payload.get("operation_id"), payload.get("generation"), payload.get("revision")) != (str(source_id), str(operation_id), generation, revision) or not isinstance(payload.get("tokens"), dict):
        raise CredentialEncryptionUnavailable("Stored GitHub grant binding is invalid")
    return payload["tokens"]


def authorization_url(settings: Settings, *, state: str, verifier: str) -> str:
    """Build GitHub's fixed authorize URL with S256 PKCE and no OAuth App scopes."""
    client = AsyncOAuth2Client(
        client_id=settings.github_app_client_id, code_challenge_method="S256"
    )
    url, _ = client.create_authorization_url(
        AUTHORIZE_URL,
        state=state,
        code_verifier=verifier,
        code_challenge_method="S256",
        redirect_uri=settings.github_app_callback_url,
    )
    return url


async def exchange_code(settings: Settings, callback_url: str, *, state: str, verifier: str) -> dict[str, Any]:
    """Exchange one single-use authorization code, requiring GitHub's rotating expiring token pair."""
    async with AsyncOAuth2Client(
        client_id=settings.github_app_client_id,
        client_secret=settings.github_app_client_secret.get_secret_value(),
        token_endpoint_auth_method="client_secret_post",
        timeout=httpx.Timeout(10),
        follow_redirects=False,
        trust_env=False,
    ) as client:
        token = await client.fetch_token(
            TOKEN_URL,
            authorization_response=callback_url,
            state=state,
            code_verifier=verifier,
            grant_type="authorization_code",
        )
    return _validate_expiring_token(token)


async def refresh_github_grant(settings: Settings, refresh_token: str) -> dict[str, Any]:
    """Rotate an expiring GitHub user grant using a fixed token endpoint and strict returned expiry fields."""
    async with AsyncOAuth2Client(
        client_id=settings.github_app_client_id,
        client_secret=settings.github_app_client_secret.get_secret_value(),
        token_endpoint_auth_method="client_secret_post",
        timeout=httpx.Timeout(10),
        follow_redirects=False,
        trust_env=False,
    ) as client:
        token = await client.refresh_token(TOKEN_URL, refresh_token=refresh_token)
    return _validate_expiring_token(token)


def _validate_expiring_token(token: dict[str, Any]) -> dict[str, Any]:
    """Reject nonexpiring, missing, malformed, or out-of-policy rotating GitHub tokens."""
    access = token.get("access_token")
    refresh = token.get("refresh_token")
    access_lifetime = token.get("expires_in")
    refresh_lifetime = token.get("refresh_token_expires_in")
    if (
        not isinstance(access, str) or not access or not isinstance(refresh, str) or not refresh
        or not isinstance(token.get("token_type", "bearer"), str) or token.get("token_type", "bearer").casefold() != "bearer"
        or isinstance(access_lifetime, bool) or not isinstance(access_lifetime, (int, float)) or not 0 < access_lifetime <= 8 * 60 * 60
        or isinstance(refresh_lifetime, bool) or not isinstance(refresh_lifetime, (int, float)) or not 0 < refresh_lifetime <= 183 * 24 * 60 * 60
    ):
        raise ValueError("github_expiring_token_required")
    now = datetime.now(UTC)
    return {"access_token": access, "refresh_token": refresh, "access_expires_at": now + timedelta(seconds=access_lifetime), "refresh_expires_at": now + timedelta(seconds=refresh_lifetime)}


async def revoke_github_grant(settings: Settings, access_token: str) -> None:
    """Revoke GitHub's app/user grant; this revokes all tokens for that app and account."""
    async with httpx.AsyncClient(timeout=httpx.Timeout(10), follow_redirects=False, trust_env=False) as client:
        response = await client.delete(
            REVOKE_URL.format(client_id=settings.github_app_client_id),
            auth=(settings.github_app_client_id, settings.github_app_client_secret.get_secret_value()),
            json={"access_token": access_token},
            headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
        )
        if response.status_code != 204:
            response.raise_for_status()


def new_pkce_pair() -> tuple[str, str, str]:
    """Create unpredictable browser state, PKCE verifier, and browser nonce values."""
    return secrets.token_urlsafe(32), secrets.token_urlsafe(64), secrets.token_urlsafe(32)


def digest(value: str) -> str:
    """Hash opaque browser/OAuth values before database persistence."""
    return hashlib.sha256(value.encode()).hexdigest()


async def _get_json_payload(path: str, token: str) -> Any:
    """Fetch one bounded fixed GitHub API response without redirects or ambient proxies."""
    if not path.startswith(("/user", "/repositories/")) or ".." in path:
        raise ValueError("github_api_path_invalid")
    async with httpx.AsyncClient(timeout=httpx.Timeout(10), follow_redirects=False, trust_env=False) as client:
        async with client.stream("GET", f"https://api.github.com{path}", headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}) as response:
            response.raise_for_status()
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > 2 * 1024 * 1024:
                    raise ValueError("github_response_too_large")
    value = httpx.Response(200, content=body).json()
    return value


async def get_json(path: str, token: str) -> dict[str, Any]:
    """Fetch one fixed GitHub API object used for identity and installation verification."""
    value = await _get_json_payload(path, token)
    if not isinstance(value, dict):
        raise ValueError("github_response_invalid")
    return value


async def probe_resource(path: str, token: str) -> None:
    """Verify an enabled repository permission using its expected bounded list response."""
    value = await _get_json_payload(path, token)
    if not isinstance(value, list) or len(value) > 100:
        raise ValueError("github_resource_response_invalid")


async def validate_granted_repository(
    config: GitHubSourceConfig, token: str, *, before_request: Callable[[], Awaitable[None]] | None = None,
) -> dict[str, str]:
    """Require an installed GitHub App and selected repository, rechecking owner authorization before each API request."""
    if before_request is not None:
        await before_request()
    user = await get_json("/user", token)
    if before_request is not None:
        await before_request()
    installations = await get_json("/user/installations?per_page=100", token)
    user_id = user.get("id")
    values = installations.get("installations")
    if not _positive_provider_id(user_id) or not isinstance(values, list) or not values or len(values) > 100:
        raise ValueError("github_app_installation_required")
    target = f"{config.github_owner}/{config.github_repository}".casefold()
    for item in values[:100]:
        installation_id = item.get("id") if isinstance(item, dict) else None
        if not _positive_provider_id(installation_id):
            continue
        if before_request is not None:
            await before_request()
        repos = await get_json(f"/user/installations/{installation_id}/repositories?per_page=100", token)
        rows = repos.get("repositories")
        if not isinstance(rows, list) or len(rows) > 100:
            raise ValueError("github_installation_repository_list_unavailable")
        match = next((repo for repo in rows if isinstance(repo, dict) and str(repo.get("full_name", "")).casefold() == target), None)
        if match:
            repo_id = match.get("id")
            owner = match.get("owner")
            owner_id = owner.get("id") if isinstance(owner, dict) else None
            if not _positive_provider_id(repo_id) or not _positive_provider_id(owner_id):
                raise ValueError("github_repository_identity_invalid")
            app_id = item.get("app_id")
            if not _positive_provider_id(app_id):
                raise ValueError("github_installation_app_identity_invalid")
            return {
                "github_user_id": str(user_id),
                "repository_id": str(repo_id),
                "owner_id": str(owner_id),
                "installation_id": str(installation_id),
                "app_id": str(app_id),
            }
    raise ValueError("github_repository_not_installed")
