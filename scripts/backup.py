"""Create a consistent age-encrypted deployment backup from the trusted host."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from modules.backup.host import BackupHostError, create_backup


def _drain_timeout(value: str) -> int:
    """Accept an explicit bounded timeout in seconds."""
    try:
        timeout = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timeout must be an integer") from exc
    if not 30 <= timeout <= 3600:
        raise argparse.ArgumentTypeError("timeout must be between 30 and 3600 seconds")
    return timeout


def main() -> int:
    """Parse bounded operator inputs, run one backup, and print its public receipt."""
    parser = argparse.ArgumentParser(description="Create an age-encrypted Umwelt-OS backup")
    parser.add_argument("--output", required=True, type=Path, help="new destination archive path")
    parser.add_argument("--drain-timeout", type=_drain_timeout, default=600)
    arguments = parser.parse_args()
    try:
        receipt = create_backup(
            arguments.output, root=REPOSITORY_ROOT, drain_timeout=arguments.drain_timeout,
        )
    except (BackupHostError, OSError) as exc:
        parser.exit(1, f"backup failed: {exc}\n")
    print(json.dumps(receipt, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
