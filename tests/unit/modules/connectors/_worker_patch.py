"""Test helper: patch a ``modules.connectors.worker`` global at every private ``_worker_*`` module that binds it.

``modules.connectors.worker`` re-exports names from its private submodules, so patching only the facade no
longer reaches call sites that live in a submodule. These wrappers set the attribute on the facade and on each
submodule holding the name, matching the pre-split behaviour of one shared module namespace.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest

from tests.unit.split_patching import patch_split, patch_split_attr


@contextmanager
def patch_worker(name: str, value: Any) -> Iterator[Any]:
    """Context-manager patch of ``name`` on the worker facade and every submodule that binds it."""
    with patch_split_attr("modules.connectors", name, value, facade="worker") as patched:
        yield patched


def monkeypatch_worker(monkeypatch: pytest.MonkeyPatch, name: str, value: Any) -> None:
    """``monkeypatch.setattr`` form of :func:`patch_worker`."""
    patch_split(monkeypatch, "modules.connectors", name, value, facade="worker")
