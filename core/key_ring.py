"""Rotation-capable Fernet key ring shared by every secret sealed with a deployment key."""

from cryptography.fernet import Fernet, MultiFernet


def key_ring(key: str) -> MultiFernet:
    """Build a cipher from ``primary`` or ``primary,secondary`` (rotation): encrypt with the first key, decrypt with any.

    Fernet keys are urlsafe base64 and never contain a comma, so a deployment key such as
    ``CONNECTOR_CREDENTIAL_ENCRYPTION_KEY`` can carry the retiring key without a new setting. With a single key the
    result encrypts and decrypts exactly like ``Fernet(key)``. Raises ValueError/UnicodeEncodeError for a missing or
    malformed key, the same errors ``Fernet`` raises, so callers keep their fail-closed handling.
    """
    return MultiFernet([Fernet(part.strip().encode("ascii")) for part in key.split(",")])
