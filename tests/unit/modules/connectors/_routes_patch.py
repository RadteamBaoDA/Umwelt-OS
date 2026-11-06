"""Test helpers: patch a ``modules.connectors.provisioning.routes`` global at every private ``_routes_*`` module that looks it up.

``routes`` re-exports names from its private submodules, so patching only the facade does not reach call
sites that live in a submodule. These set the attribute on the facade and on each submodule holding the name,
matching the pre-split behaviour of one shared module namespace.
"""

import importlib
from contextlib import ExitStack
from typing import Any
from unittest.mock import patch

import pytest

_MODULES = (
    "modules.connectors.provisioning.routes",
    "modules.connectors.provisioning._routes_common",
    "modules.connectors.provisioning._routes_config",
    "modules.connectors.provisioning._routes_activation",
)


def _holders(name: str) -> list[Any]:
    """Return every routes module that defines/imports ``name``; raise if none does."""
    found = [m for m in map(importlib.import_module, _MODULES) if hasattr(m, name)]
    if not found:
        raise AttributeError(f"no provisioning routes module has attribute {name!r}")
    return found


def patch_routes_mp(monkeypatch: pytest.MonkeyPatch, name: str, value: Any) -> None:
    """``monkeypatch.setattr`` ``name`` on the routes facade and every private module holding it."""
    for module in _holders(name):
        monkeypatch.setattr(module, name, value)


class patch_routes:
    """``patch.object`` ``name`` (same arguments) on the routes facade and every private module holding it.

    Works as a context manager and via ``start()``/``stop()`` like ``unittest.mock`` patchers.
    """

    def __init__(self, name: str, *args: Any, **kwargs: Any) -> None:
        self._patchers = [patch.object(m, name, *args, **kwargs) for m in _holders(name)]
        self._stack = ExitStack()

    def start(self) -> None:
        """Activate every underlying patcher."""
        for patcher in self._patchers:
            self._stack.enter_context(patcher)

    def stop(self) -> None:
        """Undo every patcher started by :meth:`start`."""
        self._stack.close()

    def __enter__(self) -> None:
        self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()
