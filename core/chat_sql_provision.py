"""Apply the password from ``CHAT_SQL_READER_DATABASE_URL`` to the ``umwelt_chat_sql_login`` role.

Run after ``alembic upgrade head`` (the ``migrate`` compose service does) and again to rotate the secret.
Uses the admin/direct database URL and is a no-op when the reader URL is unset (the chat SQL tool then
stays disabled). Fails when the URL names a different user than the login role.

The plaintext password never reaches the server: a SCRAM-SHA-256 verifier is computed client-side (the same
result as psql ``\\password`` / libpq ``PQencryptPasswordConn``) so server logs, SQLAlchemy exception text and
echo logs only ever see the verifier.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import os
import re
import sys

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from core.config import Settings

LOGIN_ROLE = "umwelt_chat_sql_login"
SCRAM_ITERATIONS = 4096
# libpq applies SASLprep to the password; we sidestep normalization by requiring printable ASCII, which
# SASLprep leaves unchanged. ``scripts/ensure_env.py`` generates URL-safe tokens that satisfy this.
_ASCII_PRINTABLE = re.compile(r"[\x21-\x7e]+")
# A SCRAM verifier only contains these characters, so it is safe inside a plain single-quoted literal.
_VERIFIER_CHARSET = re.compile(r"SCRAM-SHA-256\$\d+:[A-Za-z0-9+/=]+\$[A-Za-z0-9+/=]+:[A-Za-z0-9+/=]+")


def _b64(raw: bytes) -> str:
    """Return standard base64 text for ``raw``."""
    return base64.b64encode(raw).decode("ascii")


def scram_sha256_verifier(password: str, *, salt: bytes | None = None, iterations: int = SCRAM_ITERATIONS) -> str:
    """Return the PostgreSQL ``SCRAM-SHA-256$iter:salt$StoredKey:ServerKey`` verifier for ``password``.

    Args:
        password: Printable-ASCII secret (no spaces); anything else raises ``ValueError`` because SASLprep
            normalization is not implemented.
        salt: Test hook; a random 16-byte salt is generated when omitted.
        iterations: PBKDF2 iteration count (PostgreSQL default 4096).

    Raises:
        ValueError: when the password is not printable ASCII.
    """
    if not _ASCII_PRINTABLE.fullmatch(password):
        raise ValueError("chat SQL reader password must be printable ASCII without spaces")
    salt = os.urandom(16) if salt is None else salt
    salted = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.new(salted, b"Server Key", hashlib.sha256).digest()
    return f"SCRAM-SHA-256${iterations}:{_b64(salt)}${_b64(stored_key)}:{_b64(server_key)}"


async def provision(settings: Settings) -> int:
    """Set the login role's SCRAM verifier; return a process exit code (0 ok or skipped, 1 failed).

    Side effects: one ``ALTER ROLE`` on the admin connection (autocommit). Errors print only the exception
    class and SQLSTATE, never statement text, parameters or ``str(exc)``.
    """
    if not settings.chat_sql_reader_database_url:
        print("CHAT_SQL_READER_DATABASE_URL unset: chat SQL tool stays disabled.")
        return 0
    reader = make_url(settings.chat_sql_reader_database_url)
    if reader.username != LOGIN_ROLE or not reader.password:
        print(f"CHAT_SQL_READER_DATABASE_URL must log in as {LOGIN_ROLE} with a password.", file=sys.stderr)
        return 1
    try:
        verifier = scram_sha256_verifier(reader.password)
    except ValueError as exc:
        print(f"CHAT_SQL_READER_DATABASE_URL: {exc}.", file=sys.stderr)
        return 1
    if not _VERIFIER_CHARSET.fullmatch(verifier):  # guards the literal interpolation below
        raise RuntimeError("unexpected SCRAM verifier format")
    # hide_parameters keeps bind values out of SQLAlchemy exception text and echo logs.
    engine = create_async_engine(settings.admin_database_url, isolation_level="AUTOCOMMIT", hide_parameters=True)
    try:
        async with engine.connect() as conn:
            # Only the salted SCRAM verifier crosses the wire, so the statement is safe to appear in server logs;
            # no session-level SET here (the PgBouncer session-state guard forbids it).
            await conn.exec_driver_sql(f"ALTER ROLE {LOGIN_ROLE} PASSWORD '{verifier}'")
    except Exception as exc:  # noqa: BLE001 - deliberately generic; str(exc) would carry statement text
        orig = getattr(exc, "orig", None)
        sqlstate = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None) or "unknown"
        print(f"Applying the {LOGIN_ROLE} password failed: {type(exc).__name__} (SQLSTATE {sqlstate}).", file=sys.stderr)
        return 1
    finally:
        await engine.dispose()
    print(f"Password applied to {LOGIN_ROLE}.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(provision(Settings())))
