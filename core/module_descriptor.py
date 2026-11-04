"""Typed module descriptor contract shared by composition, lifecycle, worker and deletion code.

Every ``modules/**/descriptor.py`` defines a frozen subclass of ``ModuleDescriptor``. Core
reads only these declarations; it never imports a module's routes or erasure package
statically. Routers and erasure packages are named by ``"package.module[:attribute]"``
strings and resolved lazily by ``RouterSpec.load`` / ``ModuleDescriptor.load_erasure``, so
adding a capability means adding a descriptor, not editing ``apps/api/main.py`` or
``core/auth/deletion_cleanup.py`` (open/closed).
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from types import ModuleType
from typing import Any


@dataclass(frozen=True)
class RouterSpec:
    """One FastAPI router a module contributes to the API, with its global mount position.

    ``order`` is the position in the final ``include_router`` sequence across core and module
    routers (lower mounts first). Route matching is first-match-wins, so these integers encode
    the historical registration order and must not be reshuffled casually; the route-table
    baseline test guards it. ``when_enabled`` mounts the router only if the owning descriptor is
    enabled (used by the tools module, whose management API is optional).
    """

    target: str
    order: int
    when_enabled: bool = False

    def load(self) -> Any:
        """Import and return the router object named by ``target`` (``"package.module:attribute"``)."""
        module_name, _, attribute = self.target.partition(":")
        return getattr(import_module(module_name), attribute or "router")


@dataclass(frozen=True)
class ModuleDescriptor:
    """Declarative identity, wiring and lifecycle metadata for one module.

    Fields keep the names of the original per-module descriptors so existing readers
    (``.id``, ``.dependencies``, ``.scheduled_jobs``, ``.tools``, ``.enabled``) are unaffected.

    * ``dependencies``: module IDs that must be enabled for this one to be available.
    * ``scheduled_jobs``: worker entry points owned by this module (persisted dispatch gate).
    * ``routes``: public URL prefixes (documentation/gating metadata); ``routers`` are the
      importable router objects actually mounted.
    * ``erasure_module`` / ``erasure_order``: account-erasure package and its position in the
      deletion order (lower runs first; the order encodes foreign-key cleanup dependencies).
    """

    id: str = ""
    name: str = ""
    version: str = "1.0.0"
    description: str = ""
    enabled: bool = True
    dependencies: tuple[str, ...] = ()
    scheduled_jobs: tuple[str, ...] = ()
    provides: tuple[str, ...] = ()
    requires: tuple[str, ...] = ()
    routes: tuple[str, ...] = ()
    emitted_events: tuple[str, ...] = ()
    consumed_events: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    navigation: tuple[dict[str, str], ...] = ()
    settings_schema: dict[str, object] | None = None
    routers: tuple[RouterSpec, ...] = ()
    erasure_module: str | None = None
    erasure_order: int = 0

    def load_erasure(self) -> ModuleType:
        """Import and return the module's account-erasure package.

        Raises:
            LookupError: If the descriptor declares no erasure package.
        """
        if self.erasure_module is None:
            raise LookupError(f"Module {self.id} declares no erasure package")
        return import_module(self.erasure_module)
