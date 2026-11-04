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
    name = str(getattr(settings, "storage_backend", "local"))
    try:
        builder = _BUILDERS[name]
    except KeyError:
        raise ValueError(f"Unknown storage backend: {name}") from None
    return builder(settings)


register_storage_backend("local", lambda settings: LocalFilesystemBackend(settings.data_dir))
