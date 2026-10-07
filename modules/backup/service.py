"""Streaming component manifest and safe tar helpers for the host age archive runner."""

from __future__ import annotations

import hashlib
import io
import tarfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import BinaryIO
from uuid import UUID

from pydantic import ValidationError

from modules.backup.manifest import (
    BackupComponent,
    BackupFile,
    BackupManifest,
    ProtectedKeyReference,
)

CHUNK_SIZE = 1024 * 1024
MAX_ARCHIVE_MEMBERS = 100_000
MAX_EXPANDED_BYTES = 1024 * 1024 * 1024 * 500
MAX_MANIFEST_BYTES = 4 * 1024 * 1024


class BackupArchiveError(ValueError):
    """Raised for invalid metadata, damaged streams, or unsafe archive members."""


def _digest_file(path: Path) -> tuple[int, str]:
    """Return byte count and SHA-256 for a regular file, reading it in bounded chunks.

    The caller supplies a local file path. Open/read errors propagate to the caller so an
    incomplete digest can never be mistaken for a successful checksum.
    """
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        while chunk := source.read(CHUNK_SIZE):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _safe_relative(value: str) -> PurePosixPath:
    """Normalize a POSIX archive member name and reject absolute, empty, or traversing paths.

    A trailing directory slash is removed. Backslashes and empty, dot, or parent path
    segments raise ``BackupArchiveError``; only a nonempty archive-relative path is returned.
    """
    normalized = value.rstrip("/")
    path = PurePosixPath(normalized)
    parts = normalized.split("/")
    if (not normalized or path.is_absolute() or "\\" in value
            or any(part in {"", ".", ".."} for part in parts)):
        raise BackupArchiveError("Archive contains an unsafe path")
    return path


def build_manifest(
    snapshot_root: Path,
    *,
    components: dict[str, tuple[str, str]],
    required_key_references: dict[str, str],
    consistency_method: str,
    operation_id: UUID | None = None,
) -> BackupManifest:
    """Hash staged component files and return a credential-free versioned manifest."""
    snapshot_root = snapshot_root.resolve(strict=True)
    if not snapshot_root.is_dir() or snapshot_root.is_symlink():
        raise BackupArchiveError("Backup snapshot root is not a regular directory")
    if not consistency_method or len(consistency_method) > 160:
        raise BackupArchiveError("Backup consistency method is invalid")
    component_records: list[BackupComponent] = []
    component_roots: list[PurePosixPath] = []
    for name, (directory, schema_version) in sorted(components.items()):
        if not name.isidentifier() or not schema_version or len(schema_version) > 64:
            raise BackupArchiveError("Backup component metadata is invalid")
        relative_root = _safe_relative(directory)
        if any(relative_root == previous or relative_root in previous.parents
               or previous in relative_root.parents for previous in component_roots):
            raise BackupArchiveError("Backup component directories cannot overlap")
        component_roots.append(relative_root)
        root = snapshot_root.joinpath(*relative_root.parts)
        if not root.exists():
            component_records.append(BackupComponent(
                name=name, status="not_configured", directory=relative_root.as_posix(),
                schema_version=schema_version, files=(),
            ))
            continue
        if not root.is_dir() or root.is_symlink():
            raise BackupArchiveError("Backup component root is not a regular directory")
        records: list[BackupFile] = []
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                raise BackupArchiveError("Backup snapshots cannot contain symbolic links")
            if not path.is_file():
                continue
            relative = _safe_relative(path.relative_to(snapshot_root).as_posix()).as_posix()
            size, digest = _digest_file(path)
            records.append(BackupFile(path=relative, size=size, sha256=digest))
        component_records.append(BackupComponent(
            name=name, status="complete", directory=relative_root.as_posix(),
            schema_version=schema_version, files=tuple(records),
        ))
    key_references = tuple(
        ProtectedKeyReference(purpose=purpose, sha256=hashlib.sha256(reference.encode()).hexdigest())
        for purpose, reference in sorted(required_key_references.items())
    )
    return BackupManifest(
        created_at=datetime.now(UTC),
        consistency="quiesced",
        consistency_method=consistency_method,
        operation_id=operation_id,
        components=tuple(component_records),
        required_key_references=key_references,
    )


def write_snapshot_tar(
    snapshot_root: Path,
    output: BinaryIO,
    *,
    manifest: BackupManifest,
) -> None:
    """Write a gzip tar stream of staged files and the manifest without buffering the archive."""
    snapshot_root = snapshot_root.resolve(strict=True)
    expected = {
        file.path
        for component in manifest.components
        for file in component.files
    }
    actual: set[str] = set()
    for component in manifest.components:
        root = snapshot_root.joinpath(*_safe_relative(component.directory).parts)
        if component.status == "not_configured":
            if root.exists():
                raise BackupArchiveError("Backup component configuration changed after manifest creation")
            continue
        if not root.is_dir() or root.is_symlink():
            raise BackupArchiveError("Backup staging component changed after manifest creation")
        for path in root.rglob("*"):
            if path.is_symlink():
                raise BackupArchiveError("Backup snapshots cannot contain symbolic links")
            if path.is_file():
                actual.add(_safe_relative(path.relative_to(snapshot_root).as_posix()).as_posix())
    if actual != expected:
        raise BackupArchiveError("Backup staging files changed after manifest creation")
    with tarfile.open(fileobj=output, mode="w|gz", format=tarfile.PAX_FORMAT) as archive:
        for relative in sorted(expected):
            path = snapshot_root.joinpath(*_safe_relative(relative).parts)
            info = archive.gettarinfo(str(path), arcname=relative)
            if not info.isfile() or path.is_symlink():
                raise BackupArchiveError("Backup snapshots cannot contain symbolic links")
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            with path.open("rb") as source:
                archive.addfile(info, source)
        manifest_bytes = manifest.model_dump_json(indent=2).encode("utf-8")
        if len(manifest_bytes) > MAX_MANIFEST_BYTES:
            raise BackupArchiveError("Backup manifest exceeds its size limit")
        info = tarfile.TarInfo("manifest.json")
        info.size = len(manifest_bytes)
        info.mode = 0o600
        info.mtime = int(manifest.created_at.timestamp())
        archive.addfile(info, io.BytesIO(manifest_bytes))


def extract_snapshot_tar(source: BinaryIO, destination: Path) -> BackupManifest:
    """Stream-extract an age-decrypted tar safely, then verify every declared component hash."""
    if destination.exists() and any(destination.iterdir()):
        raise BackupArchiveError("Restore staging destination must be empty")
    destination.mkdir(parents=True, exist_ok=True)
    total_size = 0
    count = 0
    manifest_bytes: bytes | None = None
    try:
        with tarfile.open(fileobj=source, mode="r|gz") as archive:
            for member in archive:
                count += 1
                if count > MAX_ARCHIVE_MEMBERS:
                    raise BackupArchiveError("Backup archive has too many entries")
                safe = _safe_relative(member.name)
                if member.isdir():
                    destination.joinpath(*safe.parts).mkdir(parents=True, exist_ok=True)
                    continue
                if not member.isfile() or member.size < 0:
                    raise BackupArchiveError("Backup archive contains an unsupported entry")
                total_size += member.size
                if total_size > MAX_EXPANDED_BYTES:
                    raise BackupArchiveError("Backup archive exceeds its expanded size limit")
                file_object = archive.extractfile(member)
                if file_object is None:
                    raise BackupArchiveError("Backup archive file is incomplete")
                if safe.as_posix() == "manifest.json":
                    if manifest_bytes is not None or member.size > MAX_MANIFEST_BYTES:
                        raise BackupArchiveError("Backup manifest is duplicated or too large")
                    manifest_bytes = file_object.read(MAX_MANIFEST_BYTES + 1)
                    if len(manifest_bytes) != member.size:
                        raise BackupArchiveError("Backup manifest is incomplete")
                    continue
                target = destination.joinpath(*safe.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with file_object, target.open("xb") as output:
                    copied = 0
                    while chunk := file_object.read(CHUNK_SIZE):
                        copied += len(chunk)
                        if copied > member.size:
                            raise BackupArchiveError("Backup archive file exceeds its declared size")
                        output.write(chunk)
                    if copied != member.size:
                        raise BackupArchiveError("Backup archive file is incomplete")
        if manifest_bytes is None:
            raise BackupArchiveError("Backup manifest is missing")
        manifest = BackupManifest.model_validate_json(manifest_bytes)
    except (tarfile.TarError, ValidationError, OSError) as exc:
        raise BackupArchiveError("Backup archive could not be safely extracted") from exc
    _verify_manifest_files(destination, manifest)
    return manifest


def _verify_manifest_files(destination: Path, manifest: BackupManifest) -> None:
    """Check component checksums and reject undeclared regular files in the extracted archive."""
    declared: set[str] = set()
    for component in manifest.components:
        for item in component.files:
            relative = _safe_relative(item.path).as_posix()
            path = destination.joinpath(*PurePosixPath(relative).parts)
            if relative in declared or not path.is_file() or path.is_symlink():
                raise BackupArchiveError("Backup manifest file set is invalid")
            size, digest = _digest_file(path)
            if size != item.size or digest != item.sha256:
                raise BackupArchiveError("Backup component checksum does not match")
            declared.add(relative)
    actual = {
        path.relative_to(destination).as_posix()
        for path in destination.rglob("*") if path.is_file()
    }
    if actual != declared:
        raise BackupArchiveError("Backup contains undeclared files")
