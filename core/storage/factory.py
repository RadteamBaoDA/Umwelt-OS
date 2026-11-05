"""Storage backend selection by the ``STORAGE_BACKEND`` setting, with an open registry."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from core.storage.backend import StorageBackend
from core.storage.local import LocalFilesystemBackend

BackendBuilder = Callable[[Any], StorageBackend]

_BUILDERS: dict[str, BackendBuilder] = {}


def register_storage_backend(name: str, builder: BackendBuilder) -> None:
    """Register ``builder(settings)`` under ``name`` so ``STORAGE_BACKEND=name`` selects it.

    Re-registering a name replaces the builder (used by tests and by plug-in backends).
    """
    _BUILDERS[name] = builder


def get_storage_backend(settings: Any) -> StorageBackend:
    """Build the configured backend from ``settings.storage_backend`` (default ``local``).

    Cheap and stateless (fences are OS file locks / remote primitives), so callers may call it per
    request. Raises ``ValueError`` for an unknown backend name.
    """
    return build_storage_backend(str(getattr(settings, "storage_backend", "local")), settings)


def build_storage_backend(name: str, settings: Any) -> StorageBackend:
    """Build the backend registered as ``name`` from ``settings`` regardless of ``settings.storage_backend``.

    Used by the migration tool to open a source and a destination side by side. Raises ``ValueError``
    for an unknown name.
    """
    try:
        builder = _BUILDERS[name]
    except KeyError:
        raise ValueError(f"Unknown storage backend: {name}") from None
    return builder(settings)


register_storage_backend("local", lambda settings: LocalFilesystemBackend(settings.data_dir))


def _build_s3_backend(settings: Any) -> StorageBackend:
    """Build ``S3StorageBackend`` + ``PostgresStorageFence`` from settings (imported lazily: local users never load aioboto3)."""
    from core.storage.pg_fence import PostgresStorageFence, fence_engine
    from core.storage.s3 import S3Config, S3StorageBackend

    return S3StorageBackend(
        S3Config.from_settings(settings), PostgresStorageFence(fence_engine(settings.database_url))
    )


register_storage_backend("s3", _build_s3_backend)
