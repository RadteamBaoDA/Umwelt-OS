"""Account deletion table ownership for this module.

The authorized erasure coordinator uses these exact tables and ORM metadata to
delete bounded workspace rows in foreign-key order. No caller may infer authority
from this inventory; it must validate the persisted deletion operation first.
"""

import os
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.auth.deletion_service import authorize_account_erasure
from core.auth.erasure_contracts import AccountErasureScope
from core.config import Settings
from core.erasure_sql import AccountErasureOwner
from core.storage import storage_path
from core.storage_admission import workspace_filesystem_lease, workspace_storage_lease
from modules.knowledge.documents.models import Document, DocumentCleanupOperation

from . import models as _models  # noqa: F401 - register this owner's tables

ERASURE_TABLES: tuple[str, ...] = (
    "document_chunks",
    "document_cleanup_evidence_references",
    "document_cleanup_operations",
    "document_interactions",
    "document_versions",
    "documents",
    "normalized_document_identities",
    "normalized_version_provenance",
)

ERASURE_OWNER = AccountErasureOwner(ERASURE_TABLES)


async def erase_account_storage_page(
    ctx: dict[str, Any], scope: AccountErasureScope, cursor: dict[str, Any],
) -> tuple[bool, dict[str, Any]]:
    """Unlink one owned original by durable UUID position, preserving peer references.

    Both canonical documents and retained cleanup receipts carry raw URIs. The short existing
    raw-URI transaction lock protects the peer-reference check through local unlink, as it does
    in source purge. No network call occurs while that lock is held. An invalid path or symlink
    or Windows junction fails the stage rather than escaping the root or a peer workspace. Only the
    legacy documents layout and this workspace's documents layout are eligible. Orphan pages
    authorize even empty-directory removal, scan at most sixteen directory levels lazily and
    make at most one filesystem mutation; unexpected deeper trees fail rather than skip data.
    """
    from modules.knowledge.documents.public import lock_raw_uri_identity

    factory = cast(async_sessionmaker[AsyncSession], ctx["session_factory"])
    settings = cast(Settings, ctx["settings"])
    async with workspace_storage_lease(factory, scope.workspace_id, exclusive=True):
        async with factory() as session:
            await authorize_account_erasure(session, scope)
            await session.rollback()
        # SQL connection loss cannot release this destination lock or clear its erasure flag.
        with workspace_filesystem_lease(Path(settings.data_dir), scope.workspace_id, erase=True):
            kind = cursor.get("kind", "documents")
            if kind not in {"documents", "receipts", "orphans"}:
                raise ValueError("Invalid storage erasure checkpoint")
            after = UUID(cursor["after"]) if "after" in cursor else None
            if kind == "orphans":
                root = Path(settings.data_dir).resolve()
                lexical = root / "workspaces" / str(scope.workspace_id) / "documents"
                if any(path.is_symlink() or path.is_junction()
                       for path in (lexical, *lexical.parents) if path.is_relative_to(root)):
                    raise ValueError("Scoped original storage directory contains a symlink")
                directory = storage_path(root, f"workspaces/{scope.workspace_id}/documents")
                if not directory.exists():
                    return True, {}
                if not directory.is_dir():
                    raise ValueError("Scoped original storage root is not a directory")
                # A recursive glob can list every sibling before finding a file. Read only one
                # entry per level, so a large orphan tree cannot monopolize a worker invocation.
                entry = directory
                for _depth in range(16):
                    if entry.is_symlink() or entry.is_junction():
                        raise ValueError("Scoped original storage contains a symlink")
                    if not entry.is_dir():
                        break
                    with os.scandir(entry) as entries:
                        child = next(entries, None)
                    if child is None:
                        entry.rmdir()
                        return (True, {}) if entry == directory else (False, {"kind": "orphans"})
                    entry = Path(child.path)
                else:
                    raise ValueError("Scoped original storage exceeds the supported directory depth")
                if entry.is_symlink() or entry.is_junction():
                    raise ValueError("Scoped original storage contains a symlink")
                if not entry.is_file():
                    raise ValueError("Scoped original storage contains an unsupported entry")
                uri = entry.relative_to(root).as_posix()
                async with factory() as session:
                    await authorize_account_erasure(session, scope)
                    await lock_raw_uri_identity(session, uri)
                    peer = await session.scalar(select(Document.id).where(
                        Document.raw_uri == uri, Document.workspace_id != scope.workspace_id,
                    ).limit(1))
                    peer_receipt = await session.scalar(select(DocumentCleanupOperation.id).where(
                        DocumentCleanupOperation.raw_uri == uri,
                        DocumentCleanupOperation.workspace_id != scope.workspace_id,
                    ).limit(1))
                    if peer is not None or peer_receipt is not None:
                        raise RuntimeError("Scoped original is referenced by another workspace")
                    entry.unlink()
                    if entry.exists():
                        raise RuntimeError("Original storage unlink was not confirmed")
                    await session.rollback()
                return False, {"kind": "orphans"}
            model = Document if kind == "documents" else DocumentCleanupOperation
            async with factory() as session:
                await authorize_account_erasure(session, scope)
                query = select(model.id, model.raw_uri).where(model.workspace_id == scope.workspace_id)
                if after is not None:
                    query = query.where(model.id > after)
                row = (await session.execute(query.order_by(model.id).limit(1))).first()
                if row is None:
                    return False, {"kind": "receipts" if kind == "documents" else "orphans"}
                identifier, raw_uri = row
                if raw_uri:
                    root = Path(settings.data_dir).resolve()
                    target = storage_path(root, raw_uri)
                    lexical = root / raw_uri
                    if any(path.is_symlink() or path.is_junction()
                           for path in (lexical, *lexical.parents) if path.is_relative_to(root)):
                        raise ValueError("Original storage path contains a symlink")
                    if not (target.is_relative_to(root / "documents") or target.is_relative_to(
                        root / "workspaces" / str(scope.workspace_id) / "documents",
                    )):
                        raise ValueError("Original storage path is outside the account workspace/document layouts")
                    await lock_raw_uri_identity(session, raw_uri)
                    shared = await session.scalar(select(Document.id).where(
                        Document.raw_uri == raw_uri, Document.workspace_id != scope.workspace_id,
                    ).limit(1))
                    shared_receipt = await session.scalar(select(DocumentCleanupOperation.id).where(
                        DocumentCleanupOperation.raw_uri == raw_uri,
                        DocumentCleanupOperation.workspace_id != scope.workspace_id,
                    ).limit(1))
                    if shared is None and shared_receipt is None:
                        target.unlink(missing_ok=True)
                        if target.exists():
                            raise RuntimeError("Original storage unlink was not confirmed")
                await session.rollback()
            return False, {"kind": kind, "after": str(identifier)}
