import base64
import hashlib
import hmac
import json
from uuid import UUID

import httpx
from cryptography.fernet import Fernet, InvalidToken
from modules.connectors.public import NativeCredentialSnapshot


class CredentialEncryptionUnavailable(RuntimeError):
    """Connector secrets cannot be safely persisted with the configured key."""


def secret_fingerprint(key: str, secret: str) -> str:
    """Return a keyed SHA-256 fingerprint without persisting the plaintext secret."""
    try:
        raw_key = base64.urlsafe_b64decode(key.encode("ascii"))
        if len(raw_key) != 32:
            raise ValueError
    except (ValueError, UnicodeEncodeError) as exc:
        raise CredentialEncryptionUnavailable("Connector credential encryption is unavailable") from exc
    return hmac.new(raw_key, secret.encode("utf-8"), hashlib.sha256).hexdigest()


def encrypt_credential_input(
    key: str,
    *,
    source_id: UUID,
    slot: str,
    operation_id: UUID,
    request: dict[str, object],
    binding: dict[str, object],
) -> str:
    """Encrypt credential request and binding data scoped to source, slot, and operation."""
    try:
        cipher = Fernet(key.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise CredentialEncryptionUnavailable("Connector credential encryption is unavailable") from exc
    payload = json.dumps(
        {
            "source_id": str(source_id),
            "slot": slot,
            "operation_id": str(operation_id),
            "request": request,
            "binding": binding,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return cipher.encrypt(payload).decode("ascii")


def decrypt_credential_input(
    key: str,
    ciphertext: str,
    *,
    source_id: UUID,
    slot: str,
    operation_id: UUID,
) -> tuple[dict[str, object], dict[str, object]]:
    """Decrypt stored input only when its source, slot, operation, and payload shapes match."""
    try:
        plaintext = Fernet(key.encode("ascii")).decrypt(ciphertext.encode("ascii"))
        payload = json.loads(plaintext)
    except (ValueError, UnicodeEncodeError, InvalidToken, json.JSONDecodeError) as exc:
        raise CredentialEncryptionUnavailable("Stored connector credential input is unavailable") from exc
    if (
        payload.get("source_id") != str(source_id)
        or payload.get("slot") != slot
        or payload.get("operation_id") != str(operation_id)
        or not isinstance(payload.get("request"), dict)
        or not isinstance(payload.get("binding"), dict)
    ):
        raise CredentialEncryptionUnavailable("Stored connector credential input binding is invalid")
    return payload["request"], payload["binding"]


def encrypt_native_token(
    key: str,
    *,
    source_id: UUID,
    operation_id: UUID,
    source_generation: int,
    configuration_revision: int,
    token: str,
    verified_bot_id: str,
) -> str:
    """Encrypt a Telegram token with source, operation, revision, provider, bot, and fingerprint binding."""
    raw = token.encode("utf-8")
    if not 1 <= len(raw) <= 512 or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in token):
        raise ValueError("Telegram token is invalid")
    if not verified_bot_id.isdecimal() or len(verified_bot_id) > 20:
        raise ValueError("Telegram bot identity is invalid")
    binding = {
        "provider": "telegram",
        "source_generation": source_generation,
        "configuration_revision": configuration_revision,
        "verified_bot_id": verified_bot_id,
        "token_fingerprint": secret_fingerprint(key, token),
    }
    return encrypt_credential_input(
        key,
        source_id=source_id,
        slot="native:telegram",
        operation_id=operation_id,
        request={"token": token},
        binding=binding,
    )


def decrypt_native_token(key: str, credential: NativeCredentialSnapshot) -> str:
    """Decrypt only a ready Telegram binding whose encrypted owner metadata matches every fence."""
    if (
        credential.state != "ready" or credential.encrypted_token is None
        or credential.verified_bot_id is None
    ):
        raise CredentialEncryptionUnavailable("Stored native connector credential is unavailable")
    request, binding = decrypt_credential_input(
        key,
        credential.encrypted_token,
        source_id=credential.source_id,
        slot="native:telegram",
        operation_id=credential.operation_id,
    )
    token = request.get("token")
    if (
        not isinstance(token, str)
        or binding.get("provider") != "telegram"
        or binding.get("source_generation") != credential.source_generation
        or binding.get("configuration_revision") != credential.configuration_revision
        or binding.get("verified_bot_id") != credential.verified_bot_id
        or credential.bound_bot_id != credential.verified_bot_id
        or binding.get("token_fingerprint") != secret_fingerprint(key, token)
    ):
        raise CredentialEncryptionUnavailable("Stored native connector credential binding is invalid")
    return token


class CredentialOutcomeUnknown(RuntimeError):
    """The create request may have succeeded, but n8n did not return its ID."""


class CredentialRequestRejected(RuntimeError):
    """n8n rejected credential input before creating or updating it."""


class CredentialUpdateOutcomeUnknown(RuntimeError):
    """A known-ID update may have succeeded and can safely be retried with PATCH."""


class N8nCredentials:
    """Call n8n's credential endpoints without ambient proxy configuration."""

    def __init__(self, service_url: str, api_key: str) -> None:
        """Store the service root and API header for credential requests."""
        self._base_url = service_url.rstrip("/")
        self._headers = {"X-N8N-API-KEY": api_key}

    async def create_http_header(self, name: str, header_name: str, value: str) -> str:
        """Create an HTTP-header credential and distinguish rejected from unknown outcomes."""
        try:
            async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
                response = await client.post(
                    f"{self._base_url}/api/v1/credentials",
                    headers=self._headers,
                    json={
                        "name": name,
                        "type": "httpHeaderAuth",
                        "data": {"name": header_name, "value": value},
                    },
                )
                if response.status_code == 408 or response.status_code >= 500:
                    raise CredentialOutcomeUnknown("Credential create outcome is unknown")
                if response.is_error:
                    raise CredentialRequestRejected("n8n rejected credential creation")
                credential_id = response.json().get("id")
                if not isinstance(credential_id, str) or not credential_id:
                    raise CredentialOutcomeUnknown("Credential create response omitted its ID")
                return credential_id
        except CredentialOutcomeUnknown:
            raise
        except httpx.TransportError as exc:
            raise CredentialOutcomeUnknown("Credential create outcome is unknown") from exc

    async def rotate_http_header(
        self, credential_id: str, name: str, header_name: str, value: str
    ) -> None:
        """Update a known credential and surface transport or server outcomes as unknown."""
        try:
            async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
                response = await client.patch(
                    f"{self._base_url}/api/v1/credentials/{credential_id}",
                    headers=self._headers,
                    json={
                        "name": name,
                        "type": "httpHeaderAuth",
                        "data": {"name": header_name, "value": value},
                    },
                )
                if response.status_code == 408 or response.status_code >= 500:
                    raise CredentialUpdateOutcomeUnknown("Credential update outcome is unknown")
                if response.is_error:
                    raise CredentialRequestRejected("n8n rejected credential update")
        except CredentialUpdateOutcomeUnknown:
            raise
        except httpx.TransportError as exc:
            raise CredentialUpdateOutcomeUnknown("Credential update outcome is unknown") from exc

    async def delete(self, credential_id: str) -> None:
        """Delete a credential, treating an already-absent n8n record as success."""
        async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
            response = await client.delete(
                f"{self._base_url}/api/v1/credentials/{credential_id}",
                headers=self._headers,
            )
            if response.status_code == 404:
                return
            response.raise_for_status()
