"""Context-bound encryption and hash-only inbound bearer issuance for MCP ownership."""

import hashlib
import hmac
import json
import secrets
from uuid import UUID

from cryptography.fernet import Fernet, InvalidToken


def _cipher(key: str) -> Fernet:
    """Create the Fernet primitive from the existing deployment connector key, failing closed if absent or invalid."""
    if not key:
        raise ValueError("MCP credential encryption key is not configured")
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise ValueError("MCP credential encryption key is invalid") from exc


def encrypt_connection_credential(
    value: str, *, key: str, owner_id: int, connection_id: UUID, credential_revision: int,
) -> str:
    """Encrypt a nonempty bearer secret with authenticated domain and record/revision binding."""
    if not value or len(value.encode("utf-8")) > 8192 or credential_revision < 1:
        raise ValueError("MCP credential is empty or outside the supported bounds")
    payload = json.dumps({
        "domain": "mcp-connection", "owner_id": owner_id,
        "connection_id": str(connection_id), "credential_revision": credential_revision,
        "value": value,
    }, separators=(",", ":")).encode("utf-8")
    return _cipher(key).encrypt(payload).decode("ascii")


def decrypt_connection_credential(
    envelope: str, *, key: str, owner_id: int, connection_id: UUID, credential_revision: int,
) -> str:
    """Decrypt only for the exact owner, connection and credential revision encoded in the envelope."""
    try:
        payload = json.loads(_cipher(key).decrypt(envelope.encode("ascii")))
    except (InvalidToken, ValueError, UnicodeEncodeError, json.JSONDecodeError) as exc:
        raise ValueError("MCP credential envelope is invalid") from exc
    expected = {
        "domain": "mcp-connection", "owner_id": owner_id,
        "connection_id": str(connection_id), "credential_revision": credential_revision,
    }
    actual = {name: payload.get(name) for name in expected}
    if not hmac.compare_digest(
        json.dumps(actual, sort_keys=True, separators=(",", ":")),
        json.dumps(expected, sort_keys=True, separators=(",", ":")),
    ) or not isinstance(payload.get("value"), str) or not payload["value"]:
        raise ValueError("MCP credential envelope context does not match")
    return str(payload["value"])


def issue_inbound_token() -> tuple[str, str, str]:
    """Return a cryptographically random bearer, its SHA-256 storage digest, and safe display prefix."""
    raw = secrets.token_urlsafe(48)
    return raw, hash_inbound_token(raw), raw[:12]


def hash_inbound_token(raw: str) -> str:
    """Hash a nonempty opaque inbound token for unique database storage."""
    if not raw or len(raw) > 512:
        raise ValueError("Inbound token is invalid")
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def verify_inbound_token(raw: str, stored_hash: str) -> bool:
    """Constant-time compare an inbound bearer digest to its stored hash without logging either value."""
    if not raw or len(raw) > 512 or len(stored_hash) != 64:
        return False
    return hmac.compare_digest(hash_inbound_token(raw), stored_hash)
