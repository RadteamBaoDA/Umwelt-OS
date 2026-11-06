"""Patch helpers for owner modules whose ``public`` facade re-exports private ``_*`` submodules.

``modules.dashboard.public`` and ``modules.timeline.public`` are re-export facades, so a helper used inside the
implementation is looked up in the submodule that defines and calls it. These helpers patch the name on the facade
and on every submodule that binds it, which is equivalent to the former single-module patch.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from types import ModuleType
from typing import Any

import pytest


def _holders(package: str, name: str) -> list[ModuleType]:
    """Return the facade plus private submodules of ``package`` binding ``name``; fail loudly if none does."""
    pkg = importlib.import_module(package)
    mods = [importlib.import_module(f"{package}.public")]
    mods += [
        importlib.import_module(f"{package}.{info.name}")
        for info in pkgutil.iter_modules(pkg.__path__)
        if info.name.startswith("_")
    ]
    found = [m for m in mods if hasattr(m, name)]
    assert found, f"no {package} module defines {name}"
    return found


def patch_split(monkeypatch: pytest.MonkeyPatch, package: str, name: str, value: Any) -> None:
    """Monkeypatch ``name`` on ``package``'s facade and each private submodule that binds it."""
    for module in _holders(package, name):
        monkeypatch.setattr(module, name, value)


@contextmanager
def patch_split_attr(package: str, name: str, value: Any) -> Iterator[None]:
    """Context-manager form of :func:`patch_split` using ``unittest.mock.patch.object``."""
    from unittest.mock import patch

    with ExitStack() as stack:
        for module in _holders(package, name):
            stack.enter_context(patch.object(module, name, value))
        yield
