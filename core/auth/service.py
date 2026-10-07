from argon2 import PasswordHasher
from argon2.exceptions import VerificationError

_hasher = PasswordHasher(time_cost=2, memory_cost=19_456, parallelism=1)
# One bounded startup hash equalizes absent-email password work. It is never an account hash.
_dummy_password_hash = _hasher.hash("authentication timing placeholder; never an account credential")


def hash_password(password: str) -> str:
    """Hash an owner password with the configured Argon2 parameters."""
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """Verify an Argon2 password hash and return false for invalid or mismatched hashes."""
    try:
        return _hasher.verify(password_hash, password)
    except VerificationError:
        return False


def verify_login_password(password_hash: str | None, password: str) -> bool:
    """Perform one Argon2 verification for present or missing identities, without password scanning.

    Absent-account dummy verification always returns false even for its public placeholder.
    This reduces the obvious missing-email timing oracle; it is not a constant-time database
    or network guarantee. Uses the same bounded Argon2 parameters as real password checks.
    """
    verified = verify_password(password_hash or _dummy_password_hash, password)
    return password_hash is not None and verified
