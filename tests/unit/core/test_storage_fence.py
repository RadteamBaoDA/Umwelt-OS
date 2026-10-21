"""Destination file locks and deletion flags survive loss of SQL admission authority."""

import asyncio
import io
import subprocess
import sys
import threading
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from starlette.datastructures import UploadFile

from core import storage
from core.storage_admission import WorkspaceStorageBusy, workspace_filesystem_lease

WORKSPACE = UUID(int=40)


def test_physical_contention_is_nonblocking_and_does_not_mark_live_writer_erased(tmp_path):
    with workspace_filesystem_lease(tmp_path, WORKSPACE):
        with pytest.raises(WorkspaceStorageBusy), workspace_filesystem_lease(tmp_path, WORKSPACE, erase=True):
            pytest.fail("Erasure overtook a physical writer")
        with workspace_filesystem_lease(tmp_path, UUID(int=41)):
            pass
    assert (tmp_path / ".storage-fences" / f"{WORKSPACE}.lock").read_bytes() == b"0"


def test_physical_lease_conflicts_across_processes(tmp_path):
    child = """
import sys
from pathlib import Path
from uuid import UUID
from core.storage_admission import workspace_filesystem_lease, WorkspaceStorageBusy
try:
    with workspace_filesystem_lease(Path(sys.argv[1]), UUID(sys.argv[2]), erase=True):
        raise SystemExit(2)
except WorkspaceStorageBusy:
    raise SystemExit(0)
"""
    with workspace_filesystem_lease(tmp_path, WORKSPACE):
        result = subprocess.run([sys.executable, "-c", child, str(tmp_path), str(WORKSPACE)],
                                cwd=Path(__file__).resolve().parents[3], capture_output=True, timeout=15, check=False)
    assert result.returncode == 0


@pytest.mark.asyncio
async def test_stale_sql_admitted_upload_cannot_create_any_content_after_erasure(tmp_path):
    # The physical contract has no database dependency, so lost SQL leases cannot bypass it.
    with workspace_filesystem_lease(tmp_path, WORKSPACE, erase=True):
        pass
    for _retry in range(2):
        with pytest.raises(WorkspaceStorageBusy, match="erased"):
            await storage.save_upload(tmp_path, UploadFile(io.BytesIO(b"late-content")),
                                      uuid4(), ".txt", 100, workspace_id=WORKSPACE)
    assert not (tmp_path / "workspaces").exists()
    marker = tmp_path / ".storage-fences" / f"{WORKSPACE}.lock"
    assert marker.read_bytes() == b"1"
    with workspace_filesystem_lease(tmp_path, WORKSPACE, erase=True):
        pass
    assert marker.read_bytes() == b"1"


def test_corrupt_deletion_marker_fails_closed_in_both_directions(tmp_path):
    directory = tmp_path / ".storage-fences"
    directory.mkdir()
    marker = directory / f"{WORKSPACE}.lock"
    marker.write_bytes(b"invalid")
    for erase in (False, True):
        with pytest.raises(RuntimeError, match="invalid"), workspace_filesystem_lease(tmp_path, WORKSPACE, erase=erase):
            pytest.fail("Corrupt marker was admitted")
    assert marker.read_bytes() == b"invalid"


@pytest.mark.asyncio
async def test_writer_with_lost_sql_lease_still_blocks_erasure_until_physical_publication(tmp_path):
    started, release = asyncio.Event(), asyncio.Event()

    class PausedUpload(UploadFile):
        async def read(self, size=-1):
            if not started.is_set():
                started.set()
                await release.wait()
            return await super().read(size)

    upload = PausedUpload(io.BytesIO(b"original-before-erasure"))
    task = asyncio.create_task(storage.save_upload(tmp_path, upload, uuid4(), ".txt", 100,
                                                    workspace_id=WORKSPACE))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        # Simulate SQL connection loss: only the independent destination lock remains.
        with pytest.raises(WorkspaceStorageBusy), workspace_filesystem_lease(tmp_path, WORKSPACE, erase=True):
            pytest.fail("Erasure raced a physical upload")
    finally:
        release.set()
    uri, _size, _digest = await task
    with workspace_filesystem_lease(tmp_path, WORKSPACE, erase=True):
        (tmp_path / uri).unlink()
    with pytest.raises(WorkspaceStorageBusy):
        await storage.save_upload(tmp_path, UploadFile(io.BytesIO(b"stale-retry")),
                                  uuid4(), ".txt", 100, workspace_id=WORKSPACE)
    assert not list((tmp_path / "workspaces").rglob("*.txt"))


@pytest.mark.asyncio
async def test_cancellation_joins_thread_and_removes_temp_before_releasing_destination_lock(tmp_path, monkeypatch):
    started, finish = threading.Event(), threading.Event()
    original_write = storage._write_block

    def blocked_write(temporary, digest, block):
        started.set()
        if not finish.wait(5):
            raise TimeoutError("Test writer was not released")
        original_write(temporary, digest, block)

    monkeypatch.setattr(storage, "_write_block", blocked_write)
    task = asyncio.create_task(storage.save_upload(tmp_path, UploadFile(io.BytesIO(b"cancel-me")),
                                                    uuid4(), ".txt", 100, workspace_id=WORKSPACE))
    try:
        assert await asyncio.wait_for(asyncio.to_thread(started.wait, 5), timeout=6)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        with pytest.raises(WorkspaceStorageBusy), workspace_filesystem_lease(tmp_path, WORKSPACE, erase=True):
            pytest.fail("Cancelled physical writer was still running")
    finally:
        finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not list((tmp_path / "workspaces").rglob(".upload-*"))
    assert not list((tmp_path / "workspaces").rglob("*.txt"))
    with workspace_filesystem_lease(tmp_path, WORKSPACE, erase=True):
        pass


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "win32", reason="Windows filesystem junction")
async def test_upload_rejects_real_junction_before_creating_peer_content(tmp_path):
    import _winapi

    document = uuid4()
    peer = tmp_path / "peer-originals"
    peer.mkdir()
    directory = tmp_path / "workspaces" / str(WORKSPACE) / "documents"
    directory.mkdir(parents=True)
    junction = directory / str(document)
    _winapi.CreateJunction(str(peer), str(junction))
    try:
        with pytest.raises(ValueError, match="symlink"):
            await storage.save_upload(tmp_path, UploadFile(io.BytesIO(b"new-content")),
                                      document, ".txt", 100, workspace_id=WORKSPACE)
        assert not list(peer.iterdir())
    finally:
        junction.rmdir()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows filesystem junction")
def test_marker_directory_cannot_be_redirected_by_junction(tmp_path):
    import _winapi

    peer = tmp_path / "peer-control"
    peer.mkdir()
    junction = tmp_path / ".storage-fences"
    _winapi.CreateJunction(str(peer), str(junction))
    try:
        with pytest.raises(ValueError, match="junction"), workspace_filesystem_lease(tmp_path, WORKSPACE, erase=True):
            pytest.fail("Marker was written in a redirected directory")
        assert not list(peer.iterdir())
    finally:
        junction.rmdir()
