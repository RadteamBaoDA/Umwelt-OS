"""Patch helpers for the entities owner module, which is split into private ``_*`` submodules.

``modules.knowledge.entities.public`` is a re-export facade, so a helper used inside the implementation is looked up
in the submodule that defines and calls it. These helpers patch the name on the facade and on every submodule that
binds it, which is equivalent to the former single-module patch.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from types import ModuleType
from typing import Any

import pytest

import modules.knowledge.entities as _pkg
from modules.knowledge.entities import public as _public


def _modules() -> list[ModuleType]:
    """Return the facade plus every private implementation submodule of the entities package."""
    subs = [
        importlib.import_module(f"{_pkg.__name__}.{info.name}")
        for info in pkgutil.iter_modules(_pkg.__path__)
        if info.name.startswith("_")
    ]
    return [_public, *subs]


def _holders(name: str) -> list[ModuleType]:
    """Return modules binding ``name``; fail loudly if none does so a stale patch name cannot go silent."""
    found = [m for m in _modules() if hasattr(m, name)]
    assert found, f"no entities module defines {name}"
    return found


def patch_entities(monkeypatch: pytest.MonkeyPatch, name: str, value: Any) -> None:
    """Monkeypatch ``name`` on the facade and each entities submodule that binds it."""
    for module in _holders(name):
        monkeypatch.setattr(module, name, value)


@contextmanager
def patch_entity_attr(name: str, value: Any) -> Iterator[None]:
    """Context-manager form of :func:`patch_entities` using ``unittest.mock.patch.object``."""
    from unittest.mock import patch

    with ExitStack() as stack:
        for module in _holders(name):
            stack.enter_context(patch.object(module, name, value))
        yield
