"""Test helpers: patch a ``modules.knowledge.entities.corrections`` global at every private ``_corrections_*`` module that looks it up.

``corrections`` re-exports names from its private submodules, so patching only the facade does not reach call
sites that live in a submodule. These set the attribute on the facade and on each submodule holding the name,
matching the pre-split behaviour of one shared module namespace.
"""

import importlib
from contextlib import ExitStack
from typing import Any
from unittest.mock import patch

import pytest

_MODULES = (
    "modules.knowledge.entities.corrections",
    "modules.knowledge.entities._corrections_common",
    "modules.knowledge.entities._corrections_discover",
    "modules.knowledge.entities._corrections_delete",
    "modules.knowledge.entities._corrections_merge",
    "modules.knowledge.entities._corrections_split",
)


def _holders(name: str) -> list[Any]:
    """Return every corrections module that defines/imports ``name``; raise if none does."""
    found = [m for m in map(importlib.import_module, _MODULES) if hasattr(m, name)]
    if not found:
        raise AttributeError(f"no corrections module has attribute {name!r}")
    return found


def patch_corrections_mp(monkeypatch: pytest.MonkeyPatch, name: str, value: Any) -> None:
    """``monkeypatch.setattr`` ``name`` on the corrections facade and every private module holding it."""
    for module in _holders(name):
        monkeypatch.setattr(module, name, value)


class patch_corrections:
    """``patch.object`` ``name`` on the corrections facade and every private module holding it.

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
