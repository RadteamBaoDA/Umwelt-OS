"""Validate an encrypted backup in isolation or resume its durable operation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from modules.backup.host import (
    BackupHostError,
    cleanup_isolated_restore,
    recover_operation,
    restore_backup,
)


def main() -> int:
    """Run one typed restore, same-operation recovery, or retained-project cleanup."""
    parser = argparse.ArgumentParser(description="Restore or recover an Umwelt-OS backup")
    parser.add_argument("archive", nargs="?", type=Path, help="encrypted age backup archive")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--recover-operation", metavar="UUID", help="resume this stored backup operation")
    action.add_argument("--cleanup-project", metavar="PROJECT_ID", help="remove a retained isolated restore")
    parser.add_argument("--keep-isolated", action="store_true", help="retain the isolated project for review")
    arguments = parser.parse_args()
    if arguments.cleanup_project:
        if arguments.archive is not None or arguments.keep_isolated:
            parser.error("--cleanup-project cannot be combined with an archive or --keep-isolated")
    elif arguments.recover_operation:
        if arguments.keep_isolated:
            parser.error("--keep-isolated applies only to a new restore")
    elif arguments.archive is None:
        parser.error("an archive path is required for restore")
    try:
        if arguments.cleanup_project:
            receipt = cleanup_isolated_restore(arguments.cleanup_project, root=REPOSITORY_ROOT)
        elif arguments.recover_operation:
            receipt = recover_operation(
                arguments.recover_operation, root=REPOSITORY_ROOT,
                archive_path=arguments.archive,
            )
        else:
            receipt = restore_backup(
                arguments.archive, root=REPOSITORY_ROOT,
                keep_isolated=arguments.keep_isolated,
            )
    except (BackupHostError, OSError) as exc:
        parser.exit(1, f"restore failed: {exc}\n")
    print(json.dumps(receipt, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
