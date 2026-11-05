"""Account deletion table ownership for this module.

The authorized erasure coordinator uses these exact tables and ORM metadata to
delete bounded workspace rows in foreign-key order. No caller may infer authority
from this inventory; it must validate the persisted deletion operation first.
"""

from typing import Any, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.auth.deletion_service import authorize_account_erasure
from core.auth.erasure_contracts import AccountErasureScope
from core.config import Settings
from core.erasure_sql import AccountErasureOwner
from core.storage import get_storage_backend
from core.storage.admission import workspace_storage_lease
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
        storage = get_storage_backend(settings)
        async with storage.fence.ahold(scope.workspace_id, erase=True):
            kind = cursor.get("kind", "documents")
            if kind not in {"documents", "receipts", "orphans"}:
                raise ValueError("Invalid storage erasure checkpoint")
            after = UUID(cursor["after"]) if "after" in cursor else None
            if kind == "orphans":
                probe = await storage.aprobe_workspace_orphan(scope.workspace_id)
                if probe.state in {"absent", "pruned_root"}:
                    return True, {}
                if probe.state == "pruned_subdir":
                    return False, {"kind": "orphans"}
                uri = probe.key
                assert uri is not None  # "file" probes always carry a key
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
                    # delete(missing_ok=False) confirms absence itself (Path.exists semantics).
                    await storage.delete(uri, missing_ok=False)
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
                    storage.check_erasable(raw_uri, scope.workspace_id)
                    await lock_raw_uri_identity(session, raw_uri)
                    shared = await session.scalar(select(Document.id).where(
                        Document.raw_uri == raw_uri, Document.workspace_id != scope.workspace_id,
                    ).limit(1))
                    shared_receipt = await session.scalar(select(DocumentCleanupOperation.id).where(
                        DocumentCleanupOperation.raw_uri == raw_uri,
                        DocumentCleanupOperation.workspace_id != scope.workspace_id,
                    ).limit(1))
                    if shared is None and shared_receipt is None:
                        await storage.delete(raw_uri)
                        if await storage.exists(raw_uri):
                            raise RuntimeError("Original storage unlink was not confirmed")
                await session.rollback()
            return False, {"kind": kind, "after": str(identifier)}
