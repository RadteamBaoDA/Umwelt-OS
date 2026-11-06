"""Patch helpers for the temporal worker, which is split into private ``_worker_*`` submodules.

``modules.knowledge.temporal.worker`` is a re-export facade, so a helper used inside the implementation is looked
up in the submodule that defines and calls it. These helpers patch the name on the facade and on every
``_worker_*`` submodule that binds it, which is equivalent to the former single-module patch.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from types import ModuleType
from typing import Any
from unittest.mock import patch

import pytest

import modules.knowledge.temporal as _pkg
from modules.knowledge.temporal import worker as _facade
from tests.unit.split_patching import private_submodules


def _modules() -> list[ModuleType]:
    """Return the facade plus every private ``_worker_*`` implementation submodule."""
    return [_facade, *private_submodules(_pkg, "worker")]


def _holders(name: str) -> list[ModuleType]:
    """Return modules binding ``name``; fail loudly if none does so a stale patch name cannot go silent."""
    found = [m for m in _modules() if hasattr(m, name)]
    assert found, f"no temporal worker module defines {name}"
    return found


def patch_worker(monkeypatch: pytest.MonkeyPatch, name: str, value: Any) -> None:
    """Monkeypatch ``name`` on the facade and each temporal worker submodule that binds it."""
    for module in _holders(name):
        monkeypatch.setattr(module, name, value)


@contextmanager
def patch_worker_attr(name: str, value: Any) -> Iterator[Any]:
    """Context-manager form of :func:`patch_worker` using ``unittest.mock.patch.object``."""
    with ExitStack() as stack:
        for module in _holders(name):
            stack.enter_context(patch.object(module, name, value))
        yield value
