"""One-off copy of original files between storage backends, with checksum verification.

Usage (run from the API/worker image so settings and secrets are the deployed ones)::

    python -m core.storage.migrate --from local --to s3 --dry-run
    python -m core.storage.migrate --from local --to s3

Every canonical key (``workspaces/<ws>/documents/<doc>/<doc><suffix>``) is streamed from the source,
hashed, published on the destination through ``put_stream`` (so the destination's fence, size bound and
never-overwrite rules apply) and then re-read from the destination and compared by SHA-256 and size. Keys that
already exist on the destination are verified rather than copied, so the tool is resumable and idempotent.
Keys outside the canonical layout (legacy ``documents/...``, stray temp files) cannot be published through
``put_stream`` and are reported as skipped. The source is never modified; switching ``STORAGE_BACKEND`` and removing old files are
separate, manual steps (docs/operations.md).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import sys
from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass, field
from typing import cast
from uuid import UUID

from core.storage.backend import StorageBackend, build_document_key


@dataclass
class MigrationReport:
    """Outcome counters of one migration run; ``ok`` is true only when nothing failed or was skipped."""

    copied: int = 0
    already_present: int = 0
    skipped_other: int = 0
    bytes_copied: int = 0
    would_copy: int = 0
    failures: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Return whether every key was copied or verified (no failures and no skipped keys)."""
        return not self.failures and self.skipped_other == 0


async def _digest(storage: StorageBackend, key: str) -> tuple[int, str]:
    """Stream ``key`` from ``storage`` and return ``(size, sha256 hex)``."""
    size = 0
    digest = hashlib.sha256()
    async with contextlib.aclosing(
        cast(AsyncGenerator[bytes, None], storage.open_stream(key))
    ) as stream:
        async for block in stream:
            size += len(block)
            digest.update(block)
    return size, digest.hexdigest()


def _workspace_of(key: str) -> UUID | None:
    """Return the workspace UUID of a canonical document key, or ``None`` for any other layout."""
    parts = key.split("/")
    try:
        workspace = UUID(parts[1])
        document = UUID(parts[3])
        suffix = parts[4][len(parts[3]) :]
        return (
            workspace
            if parts[0] == "workspaces" and build_document_key(workspace, document, suffix) == key
            else None
        )
    except (ValueError, IndexError):
        return None


async def _migrate_one(
    source: StorageBackend,
    dest: StorageBackend,
    key: str,
    workspace: UUID,
    *,
    max_bytes: int,
    dry_run: bool,
    report: MigrationReport,
) -> None:
    """Copy or verify one key, recording the result in ``report`` (never raises for per-key problems)."""
    source_size, source_sha = await _digest(source, key)
    if await dest.exists(key):
        dest_size, dest_sha = await _digest(dest, key)
        if (dest_size, dest_sha) == (source_size, source_sha):
            report.already_present += 1
        else:
            report.failures.append(f"{key}: destination exists with different content")
        return
    if dry_run:
        report.would_copy += 1
        report.bytes_copied += source_size
        return
    try:
        stored = await dest.put_stream(
            key, source.open_stream(key), max_bytes, workspace_id=workspace
        )
        verified = await _digest(dest, key)
    except (
        ValueError,
        OSError,
        RuntimeError,
    ) as exc:  # includes WorkspaceStorageBusy and FileExistsError
        report.failures.append(f"{key}: {exc}")
        return
    if (stored.size, stored.sha256) != (source_size, source_sha) or verified != (
        source_size,
        source_sha,
    ):
        await dest.delete(key)  # never leave a corrupt copy behind
        report.failures.append(f"{key}: checksum mismatch after copy")
        return
    report.copied += 1
    report.bytes_copied += source_size


async def migrate_objects(
    source: StorageBackend,
    dest: StorageBackend,
    *,
    max_bytes: int,
    dry_run: bool = False,
) -> MigrationReport:
    """Copy every object of ``source`` to ``dest`` and verify it; return the :class:`MigrationReport`.

    Keys are processed sequentially in sorted order (bounded memory, deterministic resume). With
    ``dry_run`` nothing is written: the report counts what would be copied and verifies keys that
    already exist on the destination. Per-key problems are recorded, not raised, so one bad object
    does not stop the run.
    """
    report = MigrationReport()
    for key in await source.list_prefix(""):
        if key.startswith((".storage-fences/", "staging/")):
            continue  # infrastructure markers and in-flight staging objects are not originals
        workspace = _workspace_of(key)
        if workspace is None:
            report.skipped_other += 1
            continue
        await _migrate_one(
            source, dest, key, workspace, max_bytes=max_bytes, dry_run=dry_run, report=report
        )
    return report


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point: parse arguments, build both backends from ``Settings`` and print the report.

    Returns 0 when every key was copied or verified, 1 otherwise (failures or skipped legacy keys).
    """
    from core.config import Settings
    from core.storage.factory import build_storage_backend

    parser = argparse.ArgumentParser(
        prog="python -m core.storage.migrate", description=__doc__.split("\n\n")[0]
    )
    parser.add_argument(
        "--from", dest="source", required=True, help="source backend name (local or s3)"
    )
    parser.add_argument(
        "--to", dest="dest", required=True, help="destination backend name (local or s3)"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="list and verify only; write nothing"
    )
    parser.add_argument(
        "--max-bytes",
        type=int,
        default=None,
        help="per-object size bound (default UPLOAD_MAX_BYTES)",
    )
    args = parser.parse_args(argv)
    if args.source == args.dest:
        parser.error("--from and --to must differ")
    settings = Settings()
    report = asyncio.run(
        migrate_objects(
            build_storage_backend(args.source, settings),
            build_storage_backend(args.dest, settings),
            max_bytes=args.max_bytes or settings.upload_max_bytes,
            dry_run=args.dry_run,
        )
    )
    print(  # CLI output
        f"copied={report.copied} already_present={report.already_present} would_copy={report.would_copy} "
        f"skipped_other={report.skipped_other} bytes={report.bytes_copied} failures={len(report.failures)}",
    )
    for failure in report.failures:
        print(f"FAILED {failure}", file=sys.stderr)
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
