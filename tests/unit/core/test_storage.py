"""Unit tests for core storage utilities and upload management.

Tests path traversal defenses, atomic file uploads, SHA-256 calculation,
byte limit boundaries, and orphaned file garbage collection.
"""

from __future__ import annotations

import hashlib
import io
import os
import time
from pathlib import Path
from uuid import uuid4
import pytest
from starlette.datastructures import Headers, UploadFile

from core.storage import cleanup_orphaned_files, save_upload, storage_path


class TestStoragePathSecurity:
    """Test suite for path resolution and traversal security."""

    def test_storage_path_valid_relative(self, tmp_path: Path) -> None:
        """storage_path resolves legitimate relative subpaths under the storage root."""
        root = tmp_path / "storage"
        root.mkdir()
        resolved = storage_path(root, "documents/item.txt")
        assert resolved == (root.resolve() / "documents" / "item.txt")

    @pytest.mark.parametrize(
        "malicious_path",
        [
            "/absolute/path/file.txt",
            "C:\\Windows\\System32\\cmd.exe",
            "../secret.key",
            "..\\secret.key",
            "docs/../../etc/passwd",
            "docs/../secret.txt",
            "sub/../../escaping.txt",
        ],
    )
    def test_storage_path_rejects_traversal_and_absolute(
        self, tmp_path: Path, malicious_path: str
    ) -> None:
        """storage_path raises ValueError for absolute paths, '..', and '.' traversal attempts."""
        root = tmp_path / "storage"
        root.mkdir()
        with pytest.raises(ValueError, match="Invalid storage path"):
            storage_path(root, malicious_path)


class TestSaveUpload:
    """Test suite for atomic file saving, checksums, and size bounds."""

    @pytest.mark.asyncio
    async def test_save_upload_success(self, tmp_path: Path) -> None:
        """save_upload streams file content, generates sha256, and atomically creates destination."""
        root = tmp_path / "storage"
        root.mkdir()
        doc_id = uuid4()
        suffix = ".pdf"
        content = b"PDF-1.4 simulated binary document content with special characters \x00\xff"
        expected_sha256 = hashlib.sha256(content).hexdigest()

        upload = UploadFile(
            file=io.BytesIO(content),
            filename="test.pdf",
            headers=Headers({"content-type": "application/pdf"}),
        )

        rel_path, size, sha256_hash = await save_upload(
            root=root,
            upload=upload,
            document_id=doc_id,
            suffix=suffix,
            max_bytes=1024 * 1024,
        )

        assert rel_path == f"documents/{doc_id}/{doc_id}.pdf"
        assert size == len(content)
        assert sha256_hash == expected_sha256

        saved_file = root / rel_path
        assert saved_file.exists()
        assert saved_file.read_bytes() == content

        # Verify no temporary files remain in the target directory
        temp_files = list(saved_file.parent.glob(".upload-*"))
        assert len(temp_files) == 0

    @pytest.mark.asyncio
    async def test_save_upload_multi_chunk(self, tmp_path: Path) -> None:
        """save_upload correctly streams multiple 1MB chunks and verifies sha256."""
        root = tmp_path / "storage"
        root.mkdir()
        doc_id = uuid4()
        chunk_1 = b"A" * (1024 * 1024)
        chunk_2 = b"B" * (512 * 1024)
        full_content = chunk_1 + chunk_2

        upload = UploadFile(
            file=io.BytesIO(full_content),
            filename="large.bin",
        )

        rel_path, size, sha256_hash = await save_upload(
            root=root,
            upload=upload,
            document_id=doc_id,
            suffix=".bin",
            max_bytes=5 * 1024 * 1024,
        )

        assert size == len(full_content)
        assert sha256_hash == hashlib.sha256(full_content).hexdigest()
        assert (root / rel_path).read_bytes() == full_content

    @pytest.mark.asyncio
    async def test_save_upload_empty_file_rejected(self, tmp_path: Path) -> None:
        """save_upload raises ValueError when the uploaded file is zero bytes."""
        root = tmp_path / "storage"
        root.mkdir()
        doc_id = uuid4()

        upload = UploadFile(file=io.BytesIO(b""), filename="empty.txt")

        with pytest.raises(ValueError, match="Uploaded file is empty"):
            await save_upload(
                root=root,
                upload=upload,
                document_id=doc_id,
                suffix=".txt",
                max_bytes=1024,
            )

        # Destination must not exist
        dest = root / f"documents/{doc_id}/{doc_id}.txt"
        assert not dest.exists()

    @pytest.mark.asyncio
    async def test_save_upload_exceeds_max_bytes_cleans_up_temp(self, tmp_path: Path) -> None:
        """save_upload raises ValueError and removes the temp file if size limit is exceeded."""
        root = tmp_path / "storage"
        root.mkdir()
        doc_id = uuid4()
        oversized_content = b"X" * 2000

        upload = UploadFile(file=io.BytesIO(oversized_content), filename="big.txt")

        with pytest.raises(ValueError, match="Upload exceeds the configured size limit"):
            await save_upload(
                root=root,
                upload=upload,
                document_id=doc_id,
                suffix=".txt",
                max_bytes=1000,
            )

        # Ensure no orphan temp files remain
        parent_dir = root / "documents" / str(doc_id)
        if parent_dir.exists():
            temp_files = list(parent_dir.glob(".upload-*"))
            assert len(temp_files) == 0

    @pytest.mark.asyncio
    async def test_save_upload_exception_during_read_cleans_temp(self, tmp_path: Path) -> None:
        """save_upload removes the temp file when an exception occurs during reading."""
        root = tmp_path / "storage"
        root.mkdir()
        doc_id = uuid4()

        class FaultyUpload(UploadFile):
            """Upload file that raises an I/O error on second chunk."""
            def __init__(self) -> None:
                super().__init__(file=io.BytesIO(b"initial chunk"), filename="faulty.txt")
                self.calls = 0

            async def read(self, size: int = -1) -> bytes:
                self.calls += 1
                if self.calls == 1:
                    return b"some data"
                raise OSError("Simulated disk error")

        with pytest.raises(OSError, match="Simulated disk error"):
            await save_upload(
                root=root,
                upload=FaultyUpload(),
                document_id=doc_id,
                suffix=".txt",
                max_bytes=1024 * 1024,
            )

        parent_dir = root / "documents" / str(doc_id)
        if parent_dir.exists():
            assert len(list(parent_dir.glob(".upload-*"))) == 0


class TestCleanupOrphanedFiles:
    """Test suite for orphaned file garbage collection."""

    def test_cleanup_nonexistent_root(self, tmp_path: Path) -> None:
        """cleanup_orphaned_files returns 0 when root path does not exist."""
        nonexistent = tmp_path / "does_not_exist"
        assert cleanup_orphaned_files(nonexistent, set(), 60) == 0

    def test_cleanup_orphaned_files_respects_references_and_grace_period(
        self, tmp_path: Path
    ) -> None:
        """cleanup_orphaned_files removes only unreferenced files older than the grace period."""
        root = tmp_path / "storage"
        doc_dir_1 = root / "documents" / "doc1"
        doc_dir_2 = root / "documents" / "doc2"
        doc_dir_3 = root / "documents" / "doc3"
        doc_dir_1.mkdir(parents=True)
        doc_dir_2.mkdir(parents=True)
        doc_dir_3.mkdir(parents=True)

        file_old_unref = doc_dir_1 / "doc1.txt"
        file_old_ref = doc_dir_2 / "doc2.txt"
        file_new_unref = doc_dir_3 / "doc3.txt"

        file_old_unref.write_text("old unreferenced")
        file_old_ref.write_text("old referenced")
        file_new_unref.write_text("new unreferenced")

        # Set mtime for older files (older than 100 seconds)
        past_time = time.time() - 200
        os.utime(file_old_unref, (past_time, past_time))
        os.utime(file_old_ref, (past_time, past_time))

        referenced = {"documents/doc2/doc2.txt"}
        deleted_count = cleanup_orphaned_files(root, referenced, grace_seconds=60)

        assert deleted_count == 1
        assert not file_old_unref.exists()
        assert file_old_ref.exists()
        assert file_new_unref.exists()
