"""Patch helpers for the memory owner module, which is split into private ``_*`` submodules.

``modules.memory.public`` is a re-export facade, so a helper used inside the implementation is looked up in the
submodule that defines and calls it. These helpers patch the name on the facade and on every submodule that binds
it, which is equivalent to the former single-module patch.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from types import ModuleType
from typing import Any
from unittest.mock import MagicMock

import pytest

import modules.memory as _pkg
from modules.memory import public as _public

_MISSING: Any = object()


def _modules() -> list[ModuleType]:
    """Return the facade plus every private implementation submodule of the memory package."""
    subs = [
        importlib.import_module(f"{_pkg.__name__}.{info.name}")
        for info in pkgutil.iter_modules(_pkg.__path__)
        if info.name.startswith("_")
    ]
    return [_public, *subs]


def _holders(name: str) -> list[ModuleType]:
    """Return modules binding ``name``; fail loudly if none does so a stale patch name cannot go silent."""
    found = [m for m in _modules() if hasattr(m, name)]
    assert found, f"no memory module defines {name}"
    return found


def patch_memory(monkeypatch: pytest.MonkeyPatch, name: str, value: Any) -> None:
    """Monkeypatch ``name`` on the facade and each memory submodule that binds it."""
    for module in _holders(name):
        monkeypatch.setattr(module, name, value)


@contextmanager
def patch_memory_attr(name: str, value: Any = _MISSING, /, **mock_kwargs: Any) -> Iterator[Any]:
    """Context-manager form of :func:`patch_memory` using ``unittest.mock.patch.object``."""
    from unittest.mock import patch

    if value is _MISSING:
        # ``patch.object(mod, name, return_value=...)`` form: build one shared mock so every holder sees the same one.
        value = mock_kwargs.pop("new_callable", MagicMock)(**mock_kwargs)
    with ExitStack() as stack:
        for module in _holders(name):
            stack.enter_context(patch.object(module, name, value))
        yield value
