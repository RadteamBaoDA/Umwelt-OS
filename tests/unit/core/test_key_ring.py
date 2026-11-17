"""Every deployment-key cipher accepts a ``new,old`` ring so key rotation never strands sealed secrets."""

from uuid import uuid4

from cryptography.fernet import Fernet

from core.key_ring import key_ring
from modules.tools.mcp_credentials import (
    decrypt_connection_credential,
    encrypt_connection_credential,
)

OLD = Fernet.generate_key().decode()
NEW = Fernet.generate_key().decode()


def test_single_key_ring_matches_plain_fernet() -> None:
    token = Fernet(OLD.encode()).encrypt(b"x")
    assert key_ring(OLD).decrypt(token) == b"x"
    assert Fernet(OLD.encode()).decrypt(key_ring(OLD).encrypt(b"y")) == b"y"


def test_mcp_credential_survives_rotation() -> None:
    binding = {"owner_id": 1, "connection_id": uuid4(), "credential_revision": 1}
    sealed = encrypt_connection_credential("secret", key=OLD, **binding)
    assert decrypt_connection_credential(sealed, key=f"{NEW},{OLD}", **binding) == "secret"
    resealed = encrypt_connection_credential("secret", key=f"{NEW},{OLD}", **binding)
    assert decrypt_connection_credential(resealed, key=NEW, **binding) == "secret"
