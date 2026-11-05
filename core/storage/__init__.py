"""Original-file storage: backend contract, local filesystem implementation and admission fences.

Compatibility facade: ``from core.storage import storage_path`` and friends keep working. Prefer
``get_storage_backend(settings)`` for new code.
"""

from core.storage.admission import (
    WorkspaceStorageBusy,
    workspace_filesystem_lease,
    workspace_storage_lease,
)
from core.storage.backend import (
    OrphanProbe,
    StorageBackend,
    StorageFence,
    StoredObject,
    build_document_key,
    iter_upload,
)
from core.storage.factory import get_storage_backend, register_storage_backend
from core.storage.local import (
    LocalFilesystemBackend,
    LocalFilesystemFence,
    cleanup_orphaned_files,
    save_upload,
    storage_path,
)
from core.storage.pg_fence import PostgresStorageFence

__all__ = [
    "LocalFilesystemBackend",
    "LocalFilesystemFence",
    "OrphanProbe",
    "PostgresStorageFence",
    "StorageBackend",
    "StorageFence",
    "StoredObject",
    "WorkspaceStorageBusy",
    "build_document_key",
    "cleanup_orphaned_files",
    "get_storage_backend",
    "iter_upload",
    "register_storage_backend",
    "save_upload",
    "storage_path",
    "workspace_filesystem_lease",
    "workspace_storage_lease",
]
