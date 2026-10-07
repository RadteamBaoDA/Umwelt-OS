"""Trusted no-network helper for bounded named-volume snapshot and import streams."""

from __future__ import annotations

import argparse
import sys
import tarfile
from pathlib import Path

from modules.backup.service import (
    CHUNK_SIZE,
    MAX_ARCHIVE_MEMBERS,
    MAX_EXPANDED_BYTES,
    BackupArchiveError,
    _safe_relative,
)

VOLUME_ROOTS = {
    "raw_files": Path("/vol/raw_files"),
    "n8n": Path("/vol/n8n"),
    "graph": Path("/vol/graph"),
}


def export_volume(name: str) -> None:
    """Stream every regular volume file to stdout; fail closed on links or special entries."""
    root = VOLUME_ROOTS[name]
    if not root.is_dir() or root.is_symlink():
        raise BackupArchiveError("Named volume is unavailable")
    output = sys.stdout.buffer
    with tarfile.open(fileobj=output, mode="w|gz", format=tarfile.PAX_FORMAT) as archive:
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                raise BackupArchiveError("Named volume contains a symbolic link")
            if not path.is_file():
                continue
            name_in_archive = _safe_relative(path.relative_to(root).as_posix()).as_posix()
            info = archive.gettarinfo(str(path), arcname=name_in_archive)
            if not info.isfile():
                raise BackupArchiveError("Named volume contains an unsupported entry")
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            with path.open("rb") as source:
                archive.addfile(info, source)


def restore_volume(name: str) -> None:
    """Safely import one authenticated archive component into an empty isolated named volume."""
    root = VOLUME_ROOTS[name]
    if not root.is_dir() or root.is_symlink():
        raise BackupArchiveError("Isolated restore volume is unavailable")
    if any(root.iterdir()):
        raise BackupArchiveError("Isolated restore volume must be empty")
    count = 0
    total_size = 0
    with tarfile.open(fileobj=sys.stdin.buffer, mode="r|gz") as archive:
        for member in archive:
            count += 1
            if count > MAX_ARCHIVE_MEMBERS:
                raise BackupArchiveError("Component archive has too many entries")
            relative = _safe_relative(member.name)
            if member.isdir():
                root.joinpath(*relative.parts).mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile() or member.size < 0:
                raise BackupArchiveError("Component archive contains an unsupported entry")
            total_size += member.size
            if total_size > MAX_EXPANDED_BYTES:
                raise BackupArchiveError("Component archive exceeds its expanded size limit")
            source = archive.extractfile(member)
            if source is None:
                raise BackupArchiveError("Component archive file is incomplete")
            target = root.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with source, target.open("xb") as output:
                copied = 0
                while chunk := source.read(CHUNK_SIZE):
                    copied += len(chunk)
                    if copied > member.size:
                        raise BackupArchiveError("Component file exceeds its declared size")
                    output.write(chunk)
                if copied != member.size:
                    raise BackupArchiveError("Component archive file is incomplete")


def main() -> None:
    parser = argparse.ArgumentParser(description="No-network named-volume stream helper")
    parser.add_argument("action", choices=("export", "restore"))
    parser.add_argument("volume", choices=tuple(VOLUME_ROOTS))
    args = parser.parse_args()
    try:
        if args.action == "export":
            export_volume(args.volume)
        else:
            restore_volume(args.volume)
    except Exception as exc:  # noqa: BLE001  # error boundary: re-mapped to a sanitized error
        print(type(exc).__name__, file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
