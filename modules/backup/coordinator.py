"""Short-lived in-container bridge between the host runner and durable backup state."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from core.config import Settings
from modules.backup import public


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    """Run one bounded SQL control action, commit it, and return only public receipts."""
    settings = Settings()
    engine = create_async_engine(settings.database_url, pool_pre_ping=True, pool_size=1, max_overflow=0)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            if args.action == "begin":
                operation = await public.begin_operation(session, args.coordinator_id)
            elif args.action == "transition":
                operation = await public.transition_operation(
                    session, UUID(args.operation_id), args.expected, args.phase,
                    stage=args.stage, receipt=json.loads(args.receipt) if args.receipt else None,
                )
            elif args.action == "resume":
                operation = await public.resume_operation(
                    session, UUID(args.operation_id), args.outcome, archive_name=args.archive_name,
                )
            elif args.action == "operation":
                return (await public.read_operation(session, UUID(args.operation_id))).model_dump(mode="json")
            elif args.action == "release":
                operation = await public.release_operation_admission(session, UUID(args.operation_id))
            elif args.action == "recovery-required":
                operation = await public.mark_open_recovery_required(session, UUID(args.operation_id))
            elif args.action == "status":
                control = await public.read_control(session)
                revisions: list[str] = list((await session.execute(
                    text("SELECT version_num FROM alembic_version ORDER BY version_num"),
                )).scalars())
                return {"phase": control.phase, "epoch": control.epoch,
                        "operation_id": str(control.operation_id) if control.operation_id else None,
                        "active_activities": control.active_activities,
                        "uncertain_activities": control.uncertain_activities,
                        "schema_versions": revisions}
            elif args.action in {"receipt", "update-receipt"}:
                receipt = json.loads(args.receipt) if args.receipt else {}
                if args.receipt_stdin:
                    raw_receipt = sys.stdin.buffer.read(8 * 1024 * 1024 + 1)
                    if len(raw_receipt) > 8 * 1024 * 1024:
                        raise ValueError("Receipt input exceeded its bound")
                    receipt = json.loads(raw_receipt)
                receipt_action = (public.update_stage_receipt if args.action == "update-receipt"
                                  else public.record_stage_receipt)
                operation = await receipt_action(
                    session, UUID(args.operation_id), args.expected, args.stage, receipt,
                )
            else:
                raise ValueError("Unsupported backup control action")
            await session.commit()
            return operation.model_dump(mode="json")
    finally:
        await engine.dispose()


def main() -> None:
    """Accept only explicit maintenance state operations from the trusted host runner."""
    parser = argparse.ArgumentParser(description="Internal Backup state bridge")
    parser.add_argument(
        "action", choices=("begin", "transition", "resume", "status", "operation",
                           "release", "recovery-required", "receipt", "update-receipt"),
    )
    parser.add_argument("--coordinator-id", default="")
    parser.add_argument("--operation-id", default="")
    parser.add_argument("--expected", default="")
    parser.add_argument("--phase", default="")
    parser.add_argument("--stage", default=None)
    parser.add_argument("--receipt", default=None)
    parser.add_argument("--receipt-stdin", action="store_true")
    parser.add_argument("--outcome", default="")
    parser.add_argument("--archive-name", default=None)
    arguments = parser.parse_args()
    try:
        print(json.dumps(asyncio.run(_run(arguments)), separators=(",", ":")))
    except Exception as exc:  # noqa: BLE001  # deliberate boundary: failure is recorded/handled so the loop or request continues
        # Exception classes are useful to the host runner; URLs, SQL parameters and secrets are not.
        print(json.dumps({"error": type(exc).__name__}, separators=(",", ":")))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
