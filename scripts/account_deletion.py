"""Host-only status, retry and erased-bootstrap recovery commands.

Run from the repository root: python -m scripts.account_deletion status [--operation UUID]
or python -m scripts.account_deletion retry UUID. Recover erased administration with
python -m scripts.account_deletion recover-bootstrap --confirm-new-workspace.
Output never contains receipt hashes,
account IDs, workspace IDs, content, or provider credentials.
"""
import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from getpass import getpass
from uuid import UUID

from sqlalchemy import select

from core.auth.deletion_models import AccountDeletionOperation
from core.auth.recovery import recover_bootstrap_account_in_uow
from core.config import Settings
from core.database import make_session_factory


async def run(action: str, operation_id: UUID | None, *, password: str | None = None) -> None:
    """Read/retry redacted progress, or atomically recover an erased bootstrap via host authority.

    Recovery receives a terminal-only password and commits once after Auth's tombstone and
    residue checks. Any error closes the session without committing its new workspace.
    """
    settings = Settings()
    engine, factory = make_session_factory(settings.database_url)
    try:
        async with factory() as session:
            if action == "recover-bootstrap":
                if password is None:
                    raise ValueError("Recovery requires a terminal password")
                await recover_bootstrap_account_in_uow(session, password)
                await session.commit()
                print("Bootstrap recovered with a new empty private workspace; sign in with the new password.")
                return
            query = select(AccountDeletionOperation).order_by(AccountDeletionOperation.created_at.desc()).limit(100)
            if operation_id is not None:
                query = select(AccountDeletionOperation).where(AccountDeletionOperation.id == operation_id)
            if action == "retry":
                query = query.with_for_update()
            rows = list((await session.scalars(query)).all())
            if action == "retry":
                if not rows or rows[0].status != "failed":
                    raise SystemExit("Operation not found or not failed")
                rows[0].next_attempt_at = datetime.now(UTC)
                await session.commit()
            print(json.dumps([
                {"operation": str(row.id), "status": row.status, "phase": row.phase,
                 "error_code": row.error_code} for row in rows
            ]))
    finally:
        await engine.dispose()


def main() -> None:
    """Parse explicit host action; read recovery passwords without argv, echo or log output."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    status = commands.add_parser("status")
    status.add_argument("--operation", type=UUID)
    retry = commands.add_parser("retry")
    retry.add_argument("operation", type=UUID)
    recovery = commands.add_parser("recover-bootstrap")
    recovery.add_argument("--confirm-new-workspace", action="store_true", required=True)
    args = parser.parse_args()
    password = None
    if args.action == "recover-bootstrap":
        if not sys.stdin.isatty():
            parser.error("Recovery requires an interactive terminal; passwords cannot be piped")
        password = getpass("New bootstrap password (12–128 characters): ")
        if not 12 <= len(password) <= 128 or password != getpass("Repeat new password: "):
            parser.error("Passwords must match and contain 12 to 128 characters")
    try:
        asyncio.run(run(args.action, getattr(args, "operation", None), password=password))
    except ValueError as exc:
        parser.exit(1, f"{exc}\n")
    except Exception:  # noqa: BLE001 - do not expose SQL/connection credentials in CLI output
        # Database exceptions can include credential hashes/SQL parameters in their text.
        parser.exit(1, "Host command failed; inspect service availability and redacted cleanup status.\n")


if __name__ == "__main__":
    main()
