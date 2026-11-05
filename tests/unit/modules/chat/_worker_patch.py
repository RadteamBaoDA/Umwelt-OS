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
    "modules.chat._worker_base",
    "modules.chat._worker_web",
    "modules.chat._worker_purge",
    "modules.chat._worker_settle",
    "modules.chat._worker_state",
    "modules.chat._worker_setup",
    "modules.chat._worker_tools",
    "modules.chat._worker_stream",
    "modules.chat._worker_finalize",
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
