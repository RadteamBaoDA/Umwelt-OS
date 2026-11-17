"""Test helper: patch a ``modules.chat.worker`` global at every private ``_worker_*`` module that looks it up.

``modules.chat.worker`` re-exports names from its private submodules, so patching only the facade no longer
reaches call sites that live in a submodule. This sets the attribute on the facade and on each submodule that
holds the name, matching the pre-split behaviour of one shared module namespace.
"""

import importlib
from typing import Any

import pytest

_MODULES = (
    "modules.chat.worker",
    "modules.chat._worker.base",
    "modules.chat._worker.web",
    "modules.chat._worker.purge",
    "modules.chat._worker.settle",
    "modules.chat._worker.state",
    "modules.chat._worker.setup",
    "modules.chat._worker.tools",
    "modules.chat._worker.stream",
    "modules.chat._worker.finalize",
)


def patch_worker(monkeypatch: pytest.MonkeyPatch, name: str, value: Any) -> None:
    """Set ``name`` to ``value`` on the worker facade and every private worker module that defines/imports it."""
    found = False
    for module_name in _MODULES:
        module = importlib.import_module(module_name)
        if hasattr(module, name):
            monkeypatch.setattr(module, name, value)
            found = True
    if not found:
        raise AttributeError(f"no chat worker module has attribute {name!r}")
