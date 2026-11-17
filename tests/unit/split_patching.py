"""Patch helpers for owner modules whose ``public`` facade re-exports private ``_*`` submodules.

``modules.dashboard.public`` and ``modules.timeline.public`` are re-export facades, so a helper used inside the
implementation is looked up in the submodule that defines and calls it. These helpers patch the name on the facade
and on every submodule that binds it, which is equivalent to the former single-module patch.
"""

from __future__ import annotations

import importlib
import importlib.util
import pkgutil
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from types import ModuleType
from typing import Any

import pytest


def private_submodules(pkg: ModuleType, family: str | None = None) -> list[ModuleType]:
    """Return private ``_*`` submodules of ``pkg``, descending one level into private family subpackages.

    ``family`` (e.g. ``"worker"``) keeps only the ``_worker`` subpackage's modules and any flat ``_worker_*``
    siblings; ``None`` keeps every private module.
    """
    out: list[ModuleType] = []
    for info in pkgutil.iter_modules(pkg.__path__):
        name = info.name
        if not name.startswith("_") or (
            family is not None and name != f"_{family}" and not name.startswith(f"_{family}_")
        ):
            continue
        module = importlib.import_module(f"{pkg.__name__}.{name}")
        out.append(module)
        if info.ispkg:
            out += [
                importlib.import_module(f"{module.__name__}.{child.name}")
                for child in pkgutil.iter_modules(module.__path__)
            ]
    return out


def _holders(package: str, name: str, facade: str | None = "public") -> list[ModuleType]:
    """Return the facade plus private submodules of ``package`` binding ``name``; fail loudly if none does.

    ``facade`` is the facade submodule name (default ``public``); ``None`` -- or a package without a ``public``
    submodule -- means the package ``__init__`` itself is the facade (e.g. ``modules.search.indexing``).
    """
    pkg = importlib.import_module(package)
    if facade is not None and importlib.util.find_spec(f"{package}.{facade}") is None:
        facade = None
    mods = [pkg if facade is None else importlib.import_module(f"{package}.{facade}")]
    mods += private_submodules(pkg)
    found = [m for m in mods if hasattr(m, name)]
    assert found, f"no {package} module defines {name}"
    return found


def patch_split(
    monkeypatch: pytest.MonkeyPatch,
    package: str,
    name: str,
    value: Any,
    facade: str | None = "public",
) -> None:
    """Monkeypatch ``name`` on ``package``'s facade and each private submodule that binds it."""
    for module in _holders(package, name, facade):
        monkeypatch.setattr(module, name, value)


@contextmanager
def patch_split_attr(
    package: str, name: str, value: Any, facade: str | None = "public"
) -> Iterator[Any]:
    """Context-manager form of :func:`patch_split` using ``unittest.mock.patch.object``."""
    from unittest.mock import patch

    with ExitStack() as stack:
        for module in _holders(package, name, facade):
            stack.enter_context(patch.object(module, name, value))
        yield value
