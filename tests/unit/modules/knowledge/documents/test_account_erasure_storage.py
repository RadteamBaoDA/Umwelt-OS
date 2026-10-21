"""Real temporary files prove account erasure cannot skip errors or remove peer originals."""

import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

from core.auth.erasure_contracts import AccountErasureScope
from modules.knowledge.documents import erasure, public

SCOPE = AccountErasureScope(UUID(int=1), 7, UUID(int=2))
DOCUMENT_ID = UUID(int=3)
PEER_WORKSPACE = UUID(int=99)


@pytest.fixture
def storage_context(tmp_path, monkeypatch):
    session = MagicMock()
    session.execute = AsyncMock(return_value=SimpleNamespace(first=lambda: None))
    session.scalar = AsyncMock(return_value=None)
    session.rollback = AsyncMock()
    manager = MagicMock()
    manager.__aenter__ = AsyncMock(return_value=session)
    manager.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=manager)
    admission = AsyncMock()
    uri_lock = AsyncMock()
    leases = []

    @asynccontextmanager
    async def lease(_factory, workspace_id, *, exclusive=False):
        leases.append((workspace_id, exclusive))
        yield None

    monkeypatch.setattr(erasure, "authorize_account_erasure", admission)
    monkeypatch.setattr(public, "lock_raw_uri_identity", uri_lock)
    monkeypatch.setattr(erasure, "workspace_storage_lease", lease)
    ctx = {"session_factory": factory, "settings": SimpleNamespace(data_dir=tmp_path)}
    return SimpleNamespace(ctx=ctx, root=tmp_path, session=session,
                           admission=admission, uri_lock=uri_lock, leases=leases)


def write_original(context, *, workspace=SCOPE.workspace_id, name="original.txt"):
    path = context.root / "workspaces" / str(workspace) / "documents" / str(DOCUMENT_ID) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"private-original")
    return path


def canonical_row(context, path):
    uri = path.relative_to(context.root).as_posix()
    context.session.execute.return_value = SimpleNamespace(first=lambda: (DOCUMENT_ID, uri))
    return uri


@pytest.mark.asyncio
async def test_original_unlink_uses_authority_uri_lock_and_exclusive_storage_lease(storage_context):
    context = storage_context
    target = write_original(context)
    peer = write_original(context, workspace=PEER_WORKSPACE)
    uri = canonical_row(context, target)
    result = await erasure.erase_account_storage_page(context.ctx, SCOPE, {})
    assert result == (False, {"kind": "documents", "after": str(DOCUMENT_ID)})
    assert not target.exists() and peer.read_bytes() == b"private-original"
    context.admission.assert_any_await(context.session, SCOPE)
    context.uri_lock.assert_awaited_once_with(context.session, uri)
    assert context.leases == [(SCOPE.workspace_id, True)]


@pytest.mark.asyncio
async def test_missing_original_is_retry_safe_and_receipt_pages_preserve_cursor(storage_context):
    context = storage_context
    target = write_original(context)
    canonical_row(context, target)
    target.unlink()
    result = await erasure.erase_account_storage_page(context.ctx, SCOPE, {"kind": "receipts"})
    assert result == (False, {"kind": "receipts", "after": str(DOCUMENT_ID)})


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["documents", "receipts", "orphans"])
async def test_unlink_failure_never_advances_or_reports_done(storage_context, monkeypatch, kind):
    context = storage_context
    target = write_original(context)
    canonical_row(context, target)
    original_unlink = Path.unlink

    def denied(path, *args, **kwargs):
        if path == target:
            raise PermissionError("simulated storage failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", denied)
    cursor = {"kind": kind}
    with pytest.raises(PermissionError, match="storage failure"):
        await erasure.erase_account_storage_page(context.ctx, SCOPE, cursor)
    assert cursor == {"kind": kind} and target.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["documents", "orphans"])
async def test_unconfirmed_unlink_fails_instead_of_skipping_original(storage_context, monkeypatch, kind):
    context = storage_context
    target = write_original(context)
    canonical_row(context, target)
    monkeypatch.setattr(Path, "unlink", lambda *_args, **_kwargs: None)
    with pytest.raises(RuntimeError, match="not confirmed"):
        await erasure.erase_account_storage_page(context.ctx, SCOPE, {"kind": kind})
    assert target.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("shared_document,shared_receipt", [(UUID(int=8), None), (None, UUID(int=8))])
async def test_shared_legacy_original_is_preserved(storage_context, shared_document, shared_receipt):
    context = storage_context
    target = context.root / "documents" / str(DOCUMENT_ID) / "legacy.txt"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"peer-content")
    canonical_row(context, target)
    context.session.scalar.side_effect = [shared_document, shared_receipt]
    done, _cursor = await erasure.erase_account_storage_page(context.ctx, SCOPE, {})
    assert done is False and target.read_bytes() == b"peer-content"


@pytest.mark.asyncio
async def test_foreign_workspace_uri_is_rejected_even_without_peer_database_reference(storage_context):
    context = storage_context
    target = write_original(context, workspace=PEER_WORKSPACE)
    canonical_row(context, target)
    with pytest.raises(ValueError, match="workspace"):
        await erasure.erase_account_storage_page(context.ctx, SCOPE, {})
    assert target.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("uri", ["../outside.txt", "/outside.txt", "secrets/private.key"])
async def test_traversal_absolute_and_non_document_uris_cannot_be_unlinked(storage_context, uri):
    context = storage_context
    target = context.root / "secrets" / "private.key"
    target.parent.mkdir()
    target.write_bytes(b"host-secret")
    context.session.execute.return_value = SimpleNamespace(first=lambda: (DOCUMENT_ID, uri))
    with pytest.raises(ValueError):
        await erasure.erase_account_storage_page(context.ctx, SCOPE, {})
    assert target.read_bytes() == b"host-secret"


@pytest.mark.asyncio
@pytest.mark.parametrize("directory_exists", [False, True])
async def test_orphan_empty_or_missing_directory_still_requires_durable_authority(storage_context, directory_exists):
    context = storage_context
    directory = context.root / "workspaces" / str(SCOPE.workspace_id) / "documents"
    if directory_exists:
        directory.mkdir(parents=True)
    context.admission.side_effect = PermissionError("invalid deletion authority")
    with pytest.raises(PermissionError, match="authority"):
        await erasure.erase_account_storage_page(context.ctx, SCOPE, {"kind": "orphans"})
    assert directory.exists() is directory_exists


@pytest.mark.asyncio
async def test_orphan_sweep_removes_only_one_entry_per_page_then_confirms_empty(storage_context):
    context = storage_context
    targets = [write_original(context, name=f"orphan-{index}.txt") for index in range(3)]
    peer = write_original(context, workspace=PEER_WORKSPACE)
    done = False
    steps = 0
    while not done and steps < 10:
        before = sum(path.exists() for path in targets)
        done, cursor = await erasure.erase_account_storage_page(context.ctx, SCOPE, {"kind": "orphans"})
        after = sum(path.exists() for path in targets)
        assert before - after in {0, 1}
        steps += 1
    assert done and not any(path.exists() for path in targets)
    assert not (context.root / "workspaces" / str(SCOPE.workspace_id) / "documents").exists()
    assert peer.read_bytes() == b"private-original"
    assert cursor == {}


@pytest.mark.asyncio
async def test_orphan_peer_reference_blocks_completion_and_preserves_file(storage_context):
    context = storage_context
    target = write_original(context)
    context.session.scalar.side_effect = [UUID(int=8), None]
    with pytest.raises(RuntimeError, match="another workspace"):
        await erasure.erase_account_storage_page(context.ctx, SCOPE, {"kind": "orphans"})
    assert target.exists()


@pytest.mark.asyncio
async def test_orphan_directory_depth_is_bounded_without_deleting_deep_file(storage_context):
    context = storage_context
    directory = context.root / "workspaces" / str(SCOPE.workspace_id) / "documents"
    target = directory.joinpath(*("d" for _ in range(18)), "deep.txt")
    target.parent.mkdir(parents=True)
    target.write_bytes(b"too-deep")
    with pytest.raises(ValueError, match="depth"):
        await erasure.erase_account_storage_page(context.ctx, SCOPE, {"kind": "orphans"})
    assert target.read_bytes() == b"too-deep"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["documents", "orphans"])
async def test_symlink_never_unlinks_peer_target(storage_context, kind):
    context = storage_context
    peer = write_original(context, workspace=PEER_WORKSPACE)
    directory = context.root / "workspaces" / str(SCOPE.workspace_id) / "documents" / str(DOCUMENT_ID)
    directory.mkdir(parents=True)
    link = directory / "redirect.txt"
    try:
        link.symlink_to(peer)
    except OSError:
        pytest.skip("Host does not grant creation of filesystem symlinks")
    canonical_row(context, link)
    with pytest.raises(ValueError, match="symlink"):
        await erasure.erase_account_storage_page(context.ctx, SCOPE, {"kind": kind})
    assert link.is_symlink() and peer.read_bytes() == b"private-original"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["documents", "orphans"])
async def test_symlink_guard_stops_before_unlink_even_on_hosts_without_symlink_privilege(
    storage_context, monkeypatch, kind,
):
    context = storage_context
    target = write_original(context)
    canonical_row(context, target)
    original_check = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda path: path == target or original_check(path))
    with pytest.raises(ValueError, match="symlink"):
        await erasure.erase_account_storage_page(context.ctx, SCOPE, {"kind": kind})
    assert target.read_bytes() == b"private-original"


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "win32", reason="Windows filesystem junction")
@pytest.mark.parametrize("kind", ["documents", "orphans"])
async def test_real_windows_junction_cannot_redirect_erasure_into_peer_workspace(storage_context, kind):
    import _winapi

    context = storage_context
    peer = write_original(context, workspace=PEER_WORKSPACE)
    directory = context.root / "workspaces" / str(SCOPE.workspace_id) / "documents"
    directory.mkdir(parents=True)
    junction = directory / str(DOCUMENT_ID)
    _winapi.CreateJunction(str(peer.parent), str(junction))
    try:
        assert junction.is_junction() and not junction.is_symlink()
        canonical_row(context, junction / peer.name)
        with pytest.raises(ValueError, match="symlink"):
            await erasure.erase_account_storage_page(context.ctx, SCOPE, {"kind": kind})
        assert peer.read_bytes() == b"private-original"
    finally:
        junction.rmdir()  # Remove only the junction itself, never its target.


@pytest.mark.asyncio
async def test_storage_failure_is_required_coordinator_failure(storage_context, monkeypatch):
    from core.auth import deletion_cleanup

    context = storage_context
    target = write_original(context)
    monkeypatch.setattr(deletion_cleanup, "authorize_account_erasure", AsyncMock())
    monkeypatch.setattr(Path, "unlink", MagicMock(side_effect=OSError("disk unavailable")))
    with pytest.raises(deletion_cleanup.AccountErasureStageError) as failure:
        await deletion_cleanup.erase_account_data(context.ctx, SCOPE, "storage", {"kind": "orphans"})
    assert failure.value.safe_code == "storage_cleanup_failed"
    assert target.exists()
