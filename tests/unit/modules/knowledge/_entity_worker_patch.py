"""Test helper: patch a ``modules.knowledge.entities.worker`` global at every private ``_worker_*`` module binding it.

``modules.knowledge.entities.worker`` is a re-export facade, so patching only the facade no longer reaches call
sites that live in a private submodule. These wrappers set the attribute on the facade and on each submodule that
binds it, matching the pre-split behaviour of one shared module namespace.
"""

from types import ModuleType
from typing import Any

import pytest

from tests.unit.split_patching import patch_split

_PACKAGE = "modules.knowledge.entities"


def patch_entity_worker(monkeypatch: pytest.MonkeyPatch, name: str, value: Any) -> None:
    """Monkeypatch ``name`` on the entity worker facade and each ``_worker_*`` submodule that binds it."""
    patch_split(monkeypatch, _PACKAGE, name, value, facade="worker")


def patch_worker_module(
    monkeypatch: pytest.MonkeyPatch, module: ModuleType, name: str, value: Any
) -> None:
    """Patch ``name`` on ``module``; fan out to the submodules when ``module`` is the entity worker facade."""
    if module.__name__ == f"{_PACKAGE}.worker":
        patch_entity_worker(monkeypatch, name, value)
    else:
        monkeypatch.setattr(module, name, value)
