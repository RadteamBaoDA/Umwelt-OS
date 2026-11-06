"""Test helper: patch a ``modules.connectors.collection`` global at every private ``_collection_*`` module that looks it up.

``modules.connectors.collection`` re-exports names from its private submodules, so patching only the facade no
longer reaches call sites that live in a submodule. This sets the attribute on the facade and on each submodule
that holds the name, matching the pre-split behaviour of one shared module namespace.
"""

import importlib
from typing import Any

import pytest

_MODULES = (
    "modules.connectors.collection",
    "modules.connectors._collection.base",
    "modules.connectors._collection.web",
    "modules.connectors._collection.oauth",
    "modules.connectors._collection.native",
    "modules.connectors._collection.providers",
)


def patch_collection(monkeypatch: pytest.MonkeyPatch, name: str, value: Any) -> None:
    """Set ``name`` to ``value`` on the collection facade and every private module that defines/imports it."""
    found = False
    for module_name in _MODULES:
        module = importlib.import_module(module_name)
        if hasattr(module, name):
            monkeypatch.setattr(module, name, value)
            found = True
    if not found:
        raise AttributeError(f"no collection module has attribute {name!r}")
