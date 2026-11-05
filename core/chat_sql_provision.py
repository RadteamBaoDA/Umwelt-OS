"""Apply the password from ``CHAT_SQL_READER_DATABASE_URL`` to the ``umwelt_chat_sql_login`` role.

Run after ``alembic upgrade head`` (the ``migrate`` compose service does) and again to rotate the secret.
Uses the admin/direct database URL, never logs the password, and is a no-op when the reader URL is unset
(the chat SQL tool then stays disabled). Fails when the URL names a different user than the login role.
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from core.config import Settings

LOGIN_ROLE = "umwelt_chat_sql_login"


async def provision(settings: Settings) -> int:
    """Set the login role password; return a process exit code (0 ok or skipped, 1 misconfigured)."""
    if not settings.chat_sql_reader_database_url:
        print("CHAT_SQL_READER_DATABASE_URL unset: chat SQL tool stays disabled.")
        return 0
    reader = make_url(settings.chat_sql_reader_database_url)
    if reader.username != LOGIN_ROLE or not reader.password:
        print(f"CHAT_SQL_READER_DATABASE_URL must log in as {LOGIN_ROLE} with a password.", file=sys.stderr)
        return 1
    engine = create_async_engine(settings.admin_database_url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            # Utility statements take no bind parameters; let the server quote the literal via format('%L').
            stmt: str = (await conn.execute(
                text("SELECT format('ALTER ROLE " + LOGIN_ROLE + " PASSWORD %L', CAST(:pw AS text))"),  # constant role name
                {"pw": reader.password},
            )).scalar_one()
            await conn.exec_driver_sql(stmt.replace("%", "%%"))
    finally:
        await engine.dispose()
    print(f"Password applied to {LOGIN_ROLE}.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(provision(Settings())))
