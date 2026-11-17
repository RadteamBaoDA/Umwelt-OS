"""Patch helpers for the relationships owner module, which is split into private ``_*`` submodules.

``modules.knowledge.relationships.public`` is a re-export facade, so a helper used inside the implementation is
looked up in the submodule that defines and calls it. This helper patches the name on the facade and on every
submodule that binds it, which is equivalent to the former single-module patch.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from types import ModuleType
from typing import Any
from unittest.mock import patch

import pytest

import modules.knowledge.relationships as _pkg
from modules.knowledge.relationships import public as _public


def _modules() -> list[ModuleType]:
    """Return the facade plus every private implementation submodule of the relationships package."""
    subs = [
        importlib.import_module(f"{_pkg.__name__}.{info.name}")
        for info in pkgutil.iter_modules(_pkg.__path__)
        if info.name.startswith("_")
    ]
    return [_public, *subs]


def _holders(name: str) -> list[ModuleType]:
    """Return modules binding ``name``; fail loudly if none does so a stale patch name cannot go silent."""
    found = [m for m in _modules() if hasattr(m, name)]
    assert found, f"no relationships module defines {name}"
    return found


def patch_relationship(monkeypatch: pytest.MonkeyPatch, name: str, value: Any) -> None:
    """Monkeypatch ``name`` on the facade and each relationships submodule that binds it."""
    for module in _holders(name):
        monkeypatch.setattr(module, name, value)


@contextmanager
def patch_relationship_attr(name: str, value: Any) -> Iterator[Any]:
    """Patch ``name`` on the facade and each relationships submodule that binds it; yields ``value``."""
    with ExitStack() as stack:
        for module in _holders(name):
            stack.enter_context(patch.object(module, name, value))
        yield value
